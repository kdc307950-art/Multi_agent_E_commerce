"""PostgreSQL 运行时组装。

把 PostgresStore（业务数据面，同步）与 TenantScopedCheckpointer（检查点数据面，异步）
组装成 FastAPI 运行时。既有差异：`TenantScopedCheckpointer` 依赖 `AsyncConnectionPool`，
且其构造要求事件循环（`asyncio.get_running_loop`），因此 checkpointer 与迁移初始化必须在
应用 lifespan（异步上下文）内完成，不能在同步 `create_app` 里直接构造。
"""
from __future__ import annotations

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from src.core.types import DomainError, ErrorCode
from src.infrastructure.checkpointer import TenantScopedCheckpointer
from src.infrastructure.postgres_store import PostgresStore


def create_postgres_store(settings) -> PostgresStore:
    """构建同步业务 store（可在同步 create_app 中创建）。"""
    return PostgresStore(settings.database_url)


def create_postgres_pool(settings) -> AsyncConnectionPool:
    """构建异步连接池（未 open）；open 由 lifespan 在异步上下文执行。

    - `autocommit=False`：使 `set_config('app.tenant_id', ..., true)`（事务本地）生效于后续事务。
    - `row_factory=dict_row`：与 langgraph-checkpoint-postgres 的取行约定一致。
    """
    return AsyncConnectionPool(
        settings.database_url,
        min_size=1,
        max_size=settings.postgres_pool_size,
        kwargs={"autocommit": False, "row_factory": dict_row},
        open=False,  # 显式由调用方 await pool.open()，避免构造函数即 open 的弃用路径。
    )


def create_checkpointer(pool: AsyncConnectionPool) -> TenantScopedCheckpointer:
    """在事件循环内构建租户作用域 checkpointer。"""
    return TenantScopedCheckpointer(pool)


def initialize_postgres(store: PostgresStore) -> None:
    """一次性迁移：业务表 + 官方 checkpoint 表 + RLS + 函数（幂等）。"""
    from src.infrastructure import migrations

    migrations.initialize_all(store.engine)


def require_postgres_ready(store) -> None:
    """迁移后自检：确认关键 RLS 已生效，否则 fail-closed（拒绝启动/声明可用）。"""
    from sqlalchemy import text

    with store.engine.begin() as conn:
        row = conn.execute(
            text("SELECT 1 FROM pg_policies WHERE tablename='sessions' AND policyname='sessions_tenant_scope'")
        ).scalar()
    if row != 1:
        raise DomainError(ErrorCode.INTERNAL_ERROR, "会话表 RLS 未就绪，拒绝启动", 503,
                          detail={"reason": "rls_not_ready"})
