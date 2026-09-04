"""电商业务数据源契约（订单 / 物流 / 售后政策）。

设计目标（见《工具调用与集成》§2.3 / Agent 宪法）：
- `BusinessDataSource` 是业务读路径的**唯一受控入口**。`EcommerceAdapter` 通过它获取
  订单/物流/政策，禁止任何业务节点直接读 `mock_data` 或绕过租户作用域。
- 所有查询**必须服务端注入 `tenant_id`**：方法签名强制带 `tenant_id`，实现内部只允许
  返回该租户的数据；禁止无租户条件的查询。
- 资源归属校验（订单属于当前租户、customer 仅本人、staff 本租户）在 `EcommerceAdapter`/
  工具层完成；数据源仅保证**租户作用域**，不取代归属/资格校验。
- 实现必须完全自托管：Mock 仅在测试/开发环境；PostgreSQL 为生产目标（RLS + set_config 注入）。

返回值沿用业务节点的既有 dict 形状，使 `EcommerceAdapter` 的归属/资格逻辑保持稳定：
- get_order -> {tenant_id, order_id, user_id, status, total_amount, items,
                carrier, tracking_no, created_at, delivered_at}
- get_shipping -> {tenant_id, order_id, tracking_no, events}
- search_policy -> [{tenant_id, doc_id, title, content, score}]
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from src.tools import mock_data


class DataSourceError(Exception):
    """数据源系统性错误（如 PostgreSQL 不可用）；由调用方 fail-closed 转人工。"""


class BusinessDataSource(ABC):
    """电商业务数据源契约（订单 / 物流 / 政策），强制租户作用域。"""

    @abstractmethod
    def get_order(self, tenant_id: str, order_id: str) -> dict[str, Any] | None:
        """按租户取订单。返回 None 表示「不存在或不属于该租户」（不区分，避免泄露）。

        实现必须以内层 `tenant_id` 过滤；跨租户一律返回 None，绝不返回其他租户数据。
        """

    @abstractmethod
    def get_shipping(self, tenant_id: str, order_id: str) -> dict[str, Any] | None:
        """按租户取物流轨迹。返回 None 表示不存在或不属于该租户。"""

    @abstractmethod
    def search_policy(self, tenant_id: str, query: str,
                      top_k: int | None = None) -> list[dict[str, Any]]:
        """按租户检索政策文档，返回 [{tenant_id, doc_id, title, content, score}]。

        检索为空时返回 []（由 RAG 子图 fail-closed 转人工）；禁止用跨租户/默认上下文兜底。
        """

    @abstractmethod
    def policy_documents(self) -> list[dict[str, Any]]:
        """返回全部政策文档（含 tenant_id），供确定性命名的检索组件预载语料。

        仅用于本地/测试检索组件初始化；生产路径应直接走 search_policy（数据库侧过滤）。
        """

    def close(self) -> None:
        """释放底层资源（默认无操作）。"""


class MockBusinessDataSource(BusinessDataSource):
    """Mock 数据源：从 `src.tools.mock_data` 读取，仅在测试/开发环境使用。

    完全自托管（无外部依赖）。`search_policy` 复用 mock_data 的关键词检索语义，
    保证与既有测试一致的确定性行为。
    """

    def get_order(self, tenant_id: str, order_id: str) -> dict[str, Any] | None:
        return mock_data.get_order(tenant_id, order_id)

    def get_shipping(self, tenant_id: str, order_id: str) -> dict[str, Any] | None:
        return mock_data.get_shipping(tenant_id, order_id)

    def search_policy(self, tenant_id: str, query: str,
                      top_k: int | None = None) -> list[dict[str, Any]]:
        docs = mock_data.search_policy(tenant_id, query)
        if top_k is not None:
            docs = docs[:top_k]
        return docs

    def policy_documents(self) -> list[dict[str, Any]]:
        return [dict(d) for d in mock_data.KNOWLEDGE_BASE]


def build_data_source(settings) -> BusinessDataSource:
    """按配置构造业务数据源。

    - `business_data_backend=mock`（默认）：开发/测试用 Mock。
    - `business_data_backend=postgres`：生产目标，读 PostgreSQL（RLS + 服务端注入 tenant_id）。
      缺失/非法后端一律失败（fail-closed），绝不静默回退到 mock 顶替生产读路径。
    """
    backend = str(getattr(settings, "business_data_backend", "mock")).strip().lower()
    if backend == "mock":
        return MockBusinessDataSource()
    if backend == "postgres":
        from src.tools.postgres_data_source import PostgresBusinessDataSource
        return PostgresBusinessDataSource(
            database_url=str(getattr(settings, "database_url", "")),
        )
    raise DataSourceError(f"未知业务数据源后端: {backend!r}（可选 mock | postgres）")
