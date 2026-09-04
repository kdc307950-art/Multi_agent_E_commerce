"""临时 conftest：在收集 tests/ 前修补仓库迁移的 shipping_events FK 缺陷，使 PG 数据面测试可跑。

背景：仓库 `src/infrastructure/migrations.py` 的 BUSINESS_SCHEMA_SQL 中 shipping_events 的 FK
写成单列引用复合主键（`order_id ... REFERENCES orders(tenant_id, order_id)`），干净库初始化必失败。
这是**生产阻断级迁移缺陷**。本 conftest 仅在**本演练**运行时修正该条 DDL（改为复合 FK），
以便在一次性独立 PG 上跑 `tests/test_pg_callback_concurrency.py` 等 PG 标记测试（真实 RLS + 行锁）。

用法：`pytest -p evidence/prod-go-live/test-runner.pg_patch_conftest tests/test_pg_callback_concurrency.py`
（用 `-p <dotted-module>` 显式加载本插件）。
"""
from __future__ import annotations

from src.infrastructure import migrations


def _patch() -> None:
    orig = list(migrations.BUSINESS_SCHEMA_SQL)
    patched = []
    for stmt in orig:
        if "CREATE TABLE IF NOT EXISTS shipping_events" in stmt:
            patched.append(
                """
                CREATE TABLE IF NOT EXISTS shipping_events (
                    tenant_id TEXT NOT NULL REFERENCES tenants(id),
                    order_id TEXT NOT NULL,
                    tracking_no TEXT,
                    events JSONB NOT NULL DEFAULT '[]',
                    PRIMARY KEY (tenant_id, order_id),
                    FOREIGN KEY (tenant_id, order_id) REFERENCES orders(tenant_id, order_id) ON DELETE CASCADE
                )
                """
            )
        else:
            patched.append(stmt)
    migrations.BUSINESS_SCHEMA_SQL = patched


_patch()
