"""订单/物流/政策数据源的 PostgreSQL 实现（生产目标，完全自托管）

与 `PostgresStore` 复用同一数据面：SQLAlchemy + `set_config('app.tenant_id', :t, true)`
在事务内注入租户作用域，RLS policy 在同一连接/同一事务内兜底。任何缺少租户作用域
（未设置 app.tenant_id）的查询由 RLS 使其零可见。

安全：
- 查询一律带 `tenant_id` 参数并设置 `app.tenant_id`（事务本地）；RLS 强兜底。
- 返回 None 表示"不存在或不属于本租户"，不区分，避免信息泄露。
- 归属校验（customer 仅本人 / staff 本租户 / 资格窗口）仍由 `EcommerceAdapter` 完成，
  本数据源只保证租户作用域与真实性。

表结构见 `src/infrastructure/migrations.py`（orders / shipping_events / policy_documents）。
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from src.infrastructure.postgres_store import _to_sqlalchemy_url
from src.tools.data_source import BusinessDataSource, DataSourceError


class PostgresBusinessDataSource(BusinessDataSource):
    """从 PostgreSQL 读取订单/物流/政策（租户作用域 + RLS）。"""

    def __init__(self, database_url: str, *, engine: Engine | None = None) -> None:
        if engine is not None:
            self._engine = engine
        else:
            if not database_url:
                raise DataSourceError("PostgreSQL 业务数据源缺少 database_url（fail-closed）")
            self._engine = create_engine(_to_sqlalchemy_url(database_url), pool_pre_ping=True)

    def close(self) -> None:
        self._engine.dispose()

    def get_order(self, tenant_id: str, order_id: str) -> dict[str, Any] | None:
        with self._engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
            row = conn.execute(
                text("SELECT tenant_id, order_id, user_id, status, total_amount, items, "
                     "carrier, tracking_no, created_at, delivered_at "
                     "FROM orders WHERE tenant_id = :t AND order_id = :o"),
                {"t": tenant_id, "o": order_id},
            ).mappings().first()
        if row is None:
            # 不存在或不属于本租户：不区分（RLS 也使其零可见，双保险）。
            return None
        return {
            "tenant_id": row["tenant_id"],
            "order_id": row["order_id"],
            "user_id": row["user_id"],
            "status": row["status"],
            "total_amount": row["total_amount"],
            "items": row["items"] if isinstance(row["items"], list) else json.loads(row["items"] or "[]"),
            "carrier": row["carrier"],
            "tracking_no": row["tracking_no"],
            "created_at": row["created_at"],
            "delivered_at": row["delivered_at"],
        }

    def get_shipping(self, tenant_id: str, order_id: str) -> dict[str, Any] | None:
        with self._engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
            row = conn.execute(
                text("SELECT tenant_id, order_id, tracking_no, events "
                     "FROM shipping_events WHERE tenant_id = :t AND order_id = :o"),
                {"t": tenant_id, "o": order_id},
            ).mappings().first()
        if row is None:
            return None
        events = row["events"]
        return {
            "tenant_id": row["tenant_id"],
            "order_id": row["order_id"],
            "tracking_no": row["tracking_no"],
            "events": events if isinstance(events, list) else json.loads(events or "[]"),
        }

    def search_policy(self, tenant_id: str, query: str,
                      top_k: int | None = None) -> list[dict[str, Any]]:
        """数据库侧租户过滤 + 中文关键词命中评分（确定性，无外部依赖）。

        先从该租户政策文档候选，再按查询去空格后在 title/content 中的 2-gram 命中打分，
        与 `KeywordRetriever` 的确定性语义保持一致；检索为空返回 []。
        """
        with self._engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
            rows = conn.execute(
                text("SELECT tenant_id, doc_id, title, content "
                     "FROM policy_documents WHERE tenant_id = :t"),
                {"t": tenant_id},
            ).mappings().all()
        cleaned = _clean_query(query)
        scored: list[tuple[int, dict[str, Any]]] = []
        for row in rows:
            doc = {
                "tenant_id": row["tenant_id"],
                "doc_id": row["doc_id"],
                "title": row["title"],
                "content": row["content"],
            }
            if not cleaned:
                scored.append((0, doc))
                continue
            grams = {cleaned[i:i + 2] for i in range(len(cleaned) - 1)}
            hay = _clean_query(row["title"] + " " + row["content"])
            hits = sum(1 for g in grams if g in hay)
            if hits > 0:
                scored.append((hits, doc))
        scored.sort(key=lambda x: -x[0])
        k = top_k or len(scored)
        return [{**d, "score": s} for s, d in scored[:k]]

    def policy_documents(self) -> list[dict[str, Any]]:
        """拒绝无租户范围的全表预载。

        PostgreSQL 生产数据源不能提供跨租户的“全部文档”列表。Milvus 预载若没有
        已认证租户上下文会把多个租户的数据装入同一索引，违反数据面隔离；因此该
        契约在 PostgreSQL 实现中显式 fail-closed。生产检索应走 ``search_policy``，
        或由上层按单租户上下文构造独立索引。
        """
        raise DataSourceError(
            "PostgreSQL policy_documents 需要租户范围；禁止无租户全表预载（fail-closed）"
        )


def _clean_query(value: str) -> str:
    import re
    return re.sub(r"[\s，。、,:：?!？]", "", value or "")
