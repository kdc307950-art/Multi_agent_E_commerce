"""PostgreSQL RLS 数据面测试（需要真实 PostgreSQL）。

验证验收标准：跨租户查询零可见、零可写；未设置 app.tenant_id 同样零可见；
FORCE RLS 对表 owner 绕过也必须被过滤；官方 checkpoint 表只能通过
checkpoint_thread_scopes 存在性关联可见。
"""
from __future__ import annotations

import pytest
from sqlalchemy import text


def _set_tenant(conn, tenant_id: str | None) -> None:
    if tenant_id is None:
        conn.execute(text("SELECT set_config('app.tenant_id', NULL, true)"))
    else:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})


def _count(conn, sql: str, **params) -> int:
    return int(conn.execute(text(sql), params).scalar())


@pytest.mark.postgres
def test_rls_zero_visible_without_tenant(pg_app_engine):
    _seed_session(pg_app_engine, tenant_id="TENANT-A", thread_id="th-a")
    _seed_session(pg_app_engine, tenant_id="TENANT-B", thread_id="th-b")
    with pg_app_engine.begin() as conn:
        _set_tenant(conn, None)
        assert _count(conn, "SELECT count(*) FROM sessions") == 0


@pytest.mark.postgres
def test_rls_cross_tenant_zero_visible(pg_app_engine):
    _seed_session(pg_app_engine, "TENANT-A", "th-a")
    with pg_app_engine.begin() as conn:
        # 设置 A → 可见 A 的会话；设置 B → 零可见 A 的会话。
        _set_tenant(conn, "TENANT-A")
        assert _count(conn, "SELECT count(*) FROM sessions WHERE tenant_id='TENANT-A'") == 1
        _set_tenant(conn, "TENANT-B")
        assert _count(conn, "SELECT count(*) FROM sessions") == 0


@pytest.mark.postgres
def test_rls_cross_tenant_zero_write(pg_app_engine):
    with pg_app_engine.begin() as conn:
        _set_tenant(conn, "TENANT-B")
        # 在 B 的作用域下试图写入 tenant_id='TENANT-A' 的会话 → WITH CHECK 拒绝。
        with pytest.raises(Exception):
            conn.execute(text(
                "INSERT INTO sessions(tenant_id,thread_id,user_id,created_at,expires_at,last_active_at,status) "
                "VALUES('TENANT-A','th-x','u',1,1,1,'active')"
            ))


@pytest.mark.postgres
def test_checkpoint_tables_scoped_by_thread_scope(pg_app_engine):
    # 只有经 checkpoint_thread_scopes 关联的 thread 才能在对应租户下可见/可写。
    # 事务1：建租户主数据 + scope + 合法 checkpoint。
    with pg_app_engine.begin() as conn:
        _set_tenant(conn, "TENANT-A")
        conn.execute(text(
            "INSERT INTO tenants(id,name,status,created_at) VALUES('TENANT-A','A','active',1)"
        ))
        conn.execute(text(
            "INSERT INTO checkpoint_thread_scopes(tenant_id,thread_id,created_at,last_active_at,expires_at,status,cleanup_attempts) "
            "VALUES('TENANT-A','th-ck',1,1,99999,'active',0)"
        ))
        conn.execute(text(
            "INSERT INTO checkpoints(thread_id,checkpoint_id,checkpoint,metadata) "
            "VALUES('th-ck','c1','{}','{}')"
        ))
        assert _count(conn, "SELECT count(*) FROM checkpoints WHERE thread_id='th-ck'") == 1
    # 事务2：未关联作用域的线程写入被 RLS WITH CHECK 拒绝（零可写）。
    with pg_app_engine.begin() as conn:
        _set_tenant(conn, "TENANT-A")
        with pytest.raises(Exception):
            conn.execute(text(
                "INSERT INTO checkpoints(thread_id,checkpoint_id,checkpoint,metadata) "
                "VALUES('th-orphan','c2','{}','{}')"
            ))
    # 事务3、4：B 作用域 / 未设置作用域 → 零可见。
    with pg_app_engine.begin() as conn:
        _set_tenant(conn, "TENANT-B")
        assert _count(conn, "SELECT count(*) FROM checkpoints") == 0
    with pg_app_engine.begin() as conn:
        _set_tenant(conn, None)
        assert _count(conn, "SELECT count(*) FROM checkpoints") == 0


def _seed_session(engine, tenant_id: str, thread_id: str) -> None:
    """在指定租户作用域内建租户 + 成员 + 会话（迁移后的表）。"""
    from src.core.types import Role
    from src.infrastructure.postgres_store import PostgresStore
    store = PostgresStore(engine.url.render_as_string(hide_password=False), engine=engine)
    try:
        store.create_tenant(tenant_id, f"租户-{tenant_id}")
        store.add_membership(tenant_id, f"USER-{tenant_id}", Role.CUSTOMER)
        store.create_session(tenant_id, f"USER-{tenant_id}", thread_id, 1000.0, 7)
    finally:
        store.close()
