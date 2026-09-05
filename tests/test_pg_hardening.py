"""PostgreSQL 数据面补充集成测试（需要真实 PostgreSQL）。

在 `tests/test_pg_rls.py` 覆盖租户表/会话/checkpoint 隔离基础上，本文件补 t6 关注点：
1. 审计记录带 `tenant_id`，且 RLS 使**跨租户零可见**（运行角色 app_runtime / NOBYPASSRLS）。
2. 进入审计的 `detail` 不携带敏感明文（credential/hash 已在 security 层脱敏；此处验证 PG 侧
   `list_audit` 返回的 detail 保持脱敏值、唯一字段可追溯）。
3. 说明：登录**限流**状态由 memory/redis 承载（`login_rate_limit_store`），**不落 PostgreSQL**，
   故无 PG 数据面的限流状态集成测试；其隔离性已在 `tests/test_login_ratelimit.py` 内存层验证。

无可用 PostgreSQL 时（未设 DATABASE_URL / 端口不通），所有用例经 `pg_engine` fixture 自动 skip。
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from src.auth.security import audit_security_denial
from src.core.types import Role


def _count(conn, sql: str, **params) -> int:
    return int(conn.execute(text(sql), params).scalar())


def _set_tenant(conn, tenant_id: str | None) -> None:
    conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})


# ---------------------------------------------------------------------------
# 1. PostgresStore 审计带 tenant_id + 跨租户隔离（list_audit 过滤）
# ---------------------------------------------------------------------------
@pytest.mark.postgres
def test_pg_audit_tenant_scoped_via_postgres_store(pg_store):
    store = pg_store
    store.create_tenant("TENANT-A", "A")
    store.add_membership("TENANT-A", "U-A", Role.CUSTOMER)
    store.create_tenant("TENANT-B", "B")
    store.add_membership("TENANT-B", "U-B", Role.CUSTOMER)

    audit_security_denial(
        store, "TENANT-A", "U-A", "cross_user", "session", "A:U",
        {"credential": "secret", "algorithm": "argon2id"},
    )
    audit_security_denial(
        store, "TENANT-B", "U-B", "cross_tenant", "session", "B:U",
    )

    a = store.list_audit("TENANT-A")
    b = store.list_audit("TENANT-B")
    # 各租户只见本租户审计（RLS + WHERE tenant_id 双重作用域）。
    assert len(a) == 1 and a[0].tenant_id == "TENANT-A" and a[0].action == "security.deny.cross_user"
    assert len(b) == 1 and b[0].tenant_id == "TENANT-B" and b[0].action == "security.deny.cross_tenant"
    # detail 可追溯字段保留；敏感 credential 被脱敏（security 层在写入前已脱敏）。
    assert a[0].detail["reason"] == "cross_user"
    assert a[0].detail["algorithm"] == "argon2id"
    assert a[0].detail["credential"] == "[REDACTED]"


# ---------------------------------------------------------------------------
# 2. 未设租户作用域 → 审计 RLS 零可见；A 作用域 → 可见自己
# ---------------------------------------------------------------------------
@pytest.mark.postgres
def test_pg_audit_rls_zero_visible_without_tenant(pg_app_engine):
    from src.infrastructure.postgres_store import PostgresStore

    store = PostgresStore(pg_app_engine.url.render_as_string(hide_password=False), engine=pg_app_engine)
    try:
        store.create_tenant("TENANT-A", "A")
        store.add_membership("TENANT-A", "U-A", Role.CUSTOMER)
        audit_security_denial(
            store, "TENANT-A", "U-A", "z", "auth", "A:U", {"order_id": "ORD-001"},
        )
    finally:
        store.close()

    with pg_app_engine.begin() as conn:
        _set_tenant(conn, None)
        assert _count(conn, "SELECT count(*) FROM audit") == 0
    with pg_app_engine.begin() as conn:
        _set_tenant(conn, "TENANT-A")
        assert _count(conn, "SELECT count(*) FROM audit") == 1
        assert _count(conn, "SELECT count(*) FROM audit WHERE tenant_id='TENANT-A'") == 1


# ---------------------------------------------------------------------------
# 3. 跨租户写入审计被 RLS WITH CHECK 拒绝（零可写）
# ---------------------------------------------------------------------------
@pytest.mark.postgres
def test_pg_audit_cross_tenant_write_rejected(pg_app_engine):
    from src.infrastructure.postgres_store import PostgresStore

    store = PostgresStore(pg_app_engine.url.render_as_string(hide_password=False), engine=pg_app_engine)
    try:
        store.create_tenant("TENANT-A", "A")
        store.add_membership("TENANT-A", "U-A", Role.CUSTOMER)
    finally:
        store.close()
    # 在 A 作用域内显式尝试写入 tenant_id='TENANT-B' 的审计 → WITH CHECK 拒绝。
    with pg_app_engine.begin() as conn:
        _set_tenant(conn, "TENANT-A")
        with pytest.raises(Exception):
            conn.execute(text(
                "INSERT INTO audit(audit_id,tenant_id,user_id,action,target_type,target_id,detail,created_at) "
                "VALUES('x1','TENANT-B','u','a','session','t','{}',1)"
            ))
