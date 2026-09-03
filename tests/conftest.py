"""测试公共设施。"""
from __future__ import annotations

import asyncio
import sys

import pytest
from fastapi.testclient import TestClient

# Windows 默认 ProactorEventLoop 不支持 psycopg 异步；改用 SelectorEventLoop。
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from src.core.types import Role
from src.infrastructure.store import MemoryStore
from src.llm.mock import MockLLM
from src.main import create_app


@pytest.fixture
def store() -> MemoryStore:
    s = MemoryStore()
    s.create_tenant("TENANT-A", "租户A")
    s.create_tenant("TENANT-B", "租户B")
    s.add_membership("TENANT-A", "USER-001", Role.CUSTOMER)
    s.add_membership("TENANT-A", "USER-002", Role.CUSTOMER)
    s.add_membership("TENANT-A", "AGENT-A", Role.AGENT)
    s.add_membership("TENANT-A", "ADMIN-A", Role.ADMIN)
    s.add_membership("TENANT-A", "APPROVER-A", Role.APPROVER)
    s.add_membership("TENANT-B", "USER-B1", Role.CUSTOMER)
    s.add_membership("TENANT-B", "USER-B2", Role.CUSTOMER)
    s.add_membership("TENANT-B", "AGENT-B", Role.AGENT)
    s.add_membership("TENANT-B", "ADMIN-B", Role.ADMIN)
    s.add_membership("TENANT-B", "APPROVER-B", Role.APPROVER)
    return s


@pytest.fixture
def llm() -> MockLLM:
    return MockLLM()


@pytest.fixture
def app(store, llm):
    return create_app(store=store, llm=llm, seed=False)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


# ---- 便捷 helper ----
def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---- PostgreSQL 数据面（Phase 2，需 DATABASE_URL + 启动的 PostgreSQL）----
def pytest_configure(config):
    config.addinivalue_line("markers", "postgres: 需要真实 PostgreSQL（设置 DATABASE_URL）")

from sqlalchemy import create_engine as _create_engine  # noqa: E402
from src.infrastructure.postgres_store import _to_sqlalchemy_url as _norm_url  # noqa: E402
from tests.pg_helpers import (  # noqa: E402
    app_runtime_dsn,
    make_pg_engine,
    reset_pg_schema,
    setup_runtime_role,
)


@pytest.fixture
def pg_engine():
    """提供已迁移的 PostgreSQL 超级用户 engine；无可用 DB 时整组跳过。

    超级用户/表 owner 总是绕过 RLS，因此仅用于 schema 初始化与创建运行角色。
    """
    eng = make_pg_engine()
    if eng is None:
        pytest.skip("PostgreSQL 不可用：设置 DATABASE_URL 并启动数据库（docker compose up -d postgres）")
    reset_pg_schema(eng)
    setup_runtime_role(eng)  # 创建 NOBYPASSRLS 运行角色并授权（RLS 验证的前提）。
    yield eng
    from src.infrastructure import migrations
    migrations.drop_everything(eng)
    eng.dispose()


@pytest.fixture
def pg_app_engine(pg_engine):
    """用非超级用户运行角色 app_runtime（NOBYPASSRLS）连接的 engine。

    RLS 数据面测试必须用此角色；superuser/owner 会绕过 RLS，无法验证隔离。
    """
    eng = _create_engine(_norm_url(app_runtime_dsn()), pool_pre_ping=True)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def pg_store(pg_app_engine):
    from src.infrastructure.postgres_store import PostgresStore
    return PostgresStore(app_runtime_dsn(), engine=pg_app_engine)
