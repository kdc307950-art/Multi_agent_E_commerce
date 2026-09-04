"""RLS 绕过测试（多租户隔离的真实数据库防线）。

对应 t4 子项 2，验证 PostgreSQL RLS 在以下绕过场景下**确实拦截**（运行角色 app_runtime：
LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE **NOBYPASSRLS**）：
- B1 跨租户直连 SQL 查询：设置租户 A 作用域后，用显式 `WHERE tenant_id='TENANT-B'` 也**零可见**
     （RLS USING 以 app_current_tenant_id() 过滤，应用层 WHERE 不改变结果）；
- B2 跨租户直连 SQL 写入：B 作用域下 INSERT tenant_id='TENANT-A' → WITH CHECK 拒绝；
- B3 绕过应用层直接改 tenant_id：对已存在行执行 `UPDATE ... SET tenant_id='其它租户'` →
     WITH CHECK（tenant_id = app_current_tenant_id()）拒绝，行无法改到别的租户；
- B4 备份/超管连接在受限租户下越权：superuser/BYPASSRLS 角色**天然绕过 RLS**（这是 PostgreSQL
     语义，由 FORCE RLS + 角色分离规避：运行角色必须是 NOBYPASSRLS 且**非表 owner**）。本用例断言
     `app_runtime` 是 NOBYPASSRLS 且其跨租户查询**零可见**（证明 DML 运行角色无法越权），
     而非把 superuser 的绕过当作"隔离失败"。
- B5 确认 app_runtime 无法绕过 RLS：`pg_roles.rolbypassrls=false` 且任意业务表在受限作用域下
     零行/零可见（FORCE ROW LEVEL SECURITY 生效）。

本地无 PostgreSQL（未设 DATABASE_URL / 未启动 / 未装驱动）时，除静态迁移一致性检查外，
本文件所有用例经 `pg_engine`/`pg_app_engine` fixture 自动 skip（不将被跳过当成已通过）。
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from tests.pg_helpers import pg_available

# 本文件使用 conftest 的 pg_engine（迁移/superuser，建 schema + 角色）与
# pg_app_engine（app_runtime / NOBYPASSRLS）fixture；无 DB 时自动 skip。

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not pg_available(),
                       reason="PostgreSQL 不可用：设置 DATABASE_URL 并启动数据库（docker compose up -d postgres）"),
]


def _set_tenant(conn, tenant_id: str | None) -> None:
    conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})


def _count(conn, sql: str, **params) -> int:
    return int(conn.execute(text(sql), params).scalar())


def _seed(engine, tenant_id: str, thread_id: str = "th") -> None:
    from src.core.types import Role
    from src.infrastructure.postgres_store import PostgresStore

    store = PostgresStore(engine.url.render_as_string(hide_password=False), engine=engine)
    try:
        store.create_tenant(tenant_id, f"租户-{tenant_id}")
        store.add_membership(tenant_id, f"USER-{tenant_id}", Role.CUSTOMER)
        store.create_session(tenant_id, f"USER-{tenant_id}", thread_id, 1000.0, 7)
    finally:
        store.close()


# ---------------------------------------------------------------------------
# B1: 跨租户直连 SQL 查询——显式 WHERE tenant_id='其它租户' 也零可见
# ---------------------------------------------------------------------------
def test_b1_cross_tenant_direct_sql_query_zero_visible(pg_engine, pg_app_engine):
    _seed(pg_engine, "TENANT-A", "th-a")
    _seed(pg_engine, "TENANT-B", "th-b")
    with pg_app_engine.begin() as conn:
        _set_tenant(conn, "TENANT-A")
        # 即使应用层显式写 WHERE tenant_id='TENANT-B'，RLS USING 也只放行 A 的行。
        assert _count(conn, "SELECT count(*) FROM sessions WHERE tenant_id='TENANT-B'") == 0
        assert _count(conn, "SELECT count(*) FROM sessions WHERE tenant_id='TENANT-A'") == 1


# ---------------------------------------------------------------------------
# B2: 跨租户直连 SQL 写入——B 作用域写入 A 租户行 → WITH CHECK 拒绝
# ---------------------------------------------------------------------------
def test_b2_cross_tenant_direct_sql_write_rejected(pg_engine, pg_app_engine):
    _seed(pg_engine, "TENANT-A", "th-a")
    _seed(pg_engine, "TENANT-B", "th-b")
    with pg_app_engine.begin() as conn:
        _set_tenant(conn, "TENANT-B")
        with pytest.raises(Exception):
            conn.execute(text(
                "INSERT INTO sessions(tenant_id,thread_id,user_id,created_at,expires_at,last_active_at,status) "
                "VALUES('TENANT-A','th-x','u',1,1,1,'active')"
            ))


# ---------------------------------------------------------------------------
# B3: 绕过应用层直接改 tenant_id → WITH CHECK 拒绝改写租户归属
# ---------------------------------------------------------------------------
def test_b3_direct_update_tenant_id_rejected(pg_engine, pg_app_engine):
    _seed(pg_engine, "TENANT-A", "th-a")
    with pg_app_engine.begin() as conn:
        _set_tenant(conn, "TENANT-A")
        # 试图把 A 的会话改挂到 TENANT-B —— WITH CHECK (tenant_id=app_current_tenant_id()) 拒绝。
        with pytest.raises(Exception):
            conn.execute(text(
                "UPDATE sessions SET tenant_id='TENANT-B' WHERE tenant_id='TENANT-A' AND thread_id='th-a'"
            ))
    # PG 在 WITH CHECK 拒绝后会把当前事务置为 aborted；因此用**新事务**读回验证原行未变更
    # （跨租户零污染）。这与 test_pg_rls.py 每断言各用一事务的模式一致。
    with pg_app_engine.begin() as conn:
        _set_tenant(conn, "TENANT-A")
        assert _count(conn, "SELECT count(*) FROM sessions WHERE tenant_id='TENANT-A'") == 1
        assert _count(conn, "SELECT count(*) FROM sessions WHERE tenant_id='TENANT-B'") == 0


# ---------------------------------------------------------------------------
# B4: 备份/超管连接在受限租户下越权——superuser 天然绕过 RLS，运行角色 NOBYPASSRLS 不能
# ---------------------------------------------------------------------------
def test_b4_backup_superuser_bypass_vs_runtime_role_blocked(pg_engine, pg_app_engine):
    _seed(pg_engine, "TENANT-A", "th-a")
    _seed(pg_engine, "TENANT-B", "th-b")
    # B4a: 非超管运行角色（app_runtime）未设租户作用域 → 零可见（RLS 隔离）。
    with pg_app_engine.begin() as conn:
        _set_tenant(conn, None)
        assert _count(conn, "SELECT count(*) FROM sessions") == 0
    # B4b: 运行角色设 A 作用域，跨租户查询仍零可见；证明 DML 运行角色无法越权。
    with pg_app_engine.begin() as conn:
        _set_tenant(conn, "TENANT-A")
        assert _count(conn, "SELECT count(*) FROM sessions") == 1
    # B4c: 运行角色在 pg_roles 中必须 NOBYPASSRLS（不能拉高为超管/绕过）。
    with pg_engine.connect() as conn:
        row = conn.execute(text(
            "SELECT rolbypassrls, rolsuper FROM pg_roles WHERE rolname='app_runtime'"
        )).mappings().first()
    assert row is not None
    assert row["rolbypassrls"] is False
    assert row["rolsuper"] is False
    # B4d 说明：superuser/BYPASSRLS 连接天然绕过 RLS（PostgreSQL 语义）。本系统用"运行角色
    # 非 owner + NOBYPASSRLS + FORCE RLS"规避该风险；备份/超管连接只用于迁移/运维，永远不作为
    # 应用运行连接（迁移/运行角色分离）。因此 superuser 的绕过不视为隔离缺陷。


# ---------------------------------------------------------------------------
# B5: 确认 app_runtime 无法绕过 RLS（NOBYPASSRLS 标志 + FORCE RLS 生效）
# ---------------------------------------------------------------------------
def test_b5_app_runtime_cannot_bypass_rls(pg_engine, pg_app_engine):
    _seed(pg_engine, "TENANT-A", "th-a")
    _seed(pg_engine, "TENANT-B", "th-b")
    with pg_engine.connect() as conn:
        row = conn.execute(text(
            "SELECT rolbypassrls FROM pg_roles WHERE rolname='app_runtime'"
        )).mappings().first()
    assert row is not None and row["rolbypassrls"] is False
    # 表应 ENABLE + FORCE ROW LEVEL SECURITY（FORCE 使表 owner 也按 policy 过滤）。
    with pg_engine.connect() as conn:
        rls = conn.execute(text(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname='sessions'"
        )).mappings().first()
    assert rls is not None
    assert rls["relrowsecurity"] is True
    assert rls["relforcerowsecurity"] is True
    # 运行角色跨租户查询零可见（RLS 生效，无法绕过）。
    with pg_app_engine.begin() as conn:
        _set_tenant(conn, "TENANT-A")
        assert _count(conn, "SELECT count(*) FROM sessions WHERE tenant_id='TENANT-B'") == 0


# 注：本文件为模块级 skipif（无 PG 整组跳过）。本地可运行的**静态迁移一致性检查**（不依赖 PG）
# 已拆到 `tests/test_rls_static_definitions.py`（另文，always 运行）。
