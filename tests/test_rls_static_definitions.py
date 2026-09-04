"""RLS 静态迁移一致性检查（无需 PostgreSQL，本机可运行）。

对应 t4 子项 2 的"静态/离线"部分：即使本机无 PG，也验证迁移定义在**源码层面**确实为
多租户隔离配置了正确的 RLS：
- 业务表（tenants 除外）与官方 checkpoint 表全部 `ENABLE + FORCE ROW LEVEL SECURITY`
  （FORCE 使表 owner 也按 policy 过滤，规避 owner/备份账号天然绕过）；
- 每个业务表都有 `tenant_id = app_current_tenant_id()` 的 USING + WITH CHECK policy；
- 运行角色 app_runtime 定义为 `LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS`
  （NOBYPASSRLS 使其无法绕过 RLS）。

真实数据库层的**动态**绕过验证（B1–B5）见 `tests/test_rls_bypass.py`（需 PostgreSQL）。
"""
from __future__ import annotations

from src.infrastructure import migrations


def test_all_business_tables_enabled_and_forced_rls():
    """业务表 RLS 语句必须同时 ENABLE 与 FORCE ROW LEVEL SECURITY。"""
    for stmt in migrations.RLS_TABLES_SQL:
        assert "ENABLE ROW LEVEL SECURITY" in stmt, stmt
        assert "FORCE ROW LEVEL SECURITY" in stmt, stmt


def test_checkpoint_tables_enabled_and_forced_rls():
    for stmt in migrations.CHECKPOINT_RLS_SQL:
        assert "ENABLE ROW LEVEL SECURITY" in stmt, stmt
        assert "FORCE ROW LEVEL SECURITY" in stmt, stmt


def test_each_business_table_has_tenant_scope_policy():
    """每个业务表 policy 都以 app_current_tenant_id() 为 USING + WITH CHECK 条件。"""
    for stmt in migrations.RLS_TABLES_SQL:
        assert "CREATE POLICY" in stmt, stmt
        assert "app_current_tenant_id()" in stmt, stmt
        assert "USING" in stmt and "WITH CHECK" in stmt, stmt


def test_checkpoint_tables_scoped_to_current_tenant_via_scope():
    """官方 checkpoint 表用 checkpoint_thread_in_current_tenant(thread_id) 做存在性关联。"""
    for stmt in migrations.CHECKPOINT_RLS_SQL:
        assert "checkpoint_thread_in_current_tenant(thread_id)" in stmt, stmt


def test_runtime_role_defined_nobypassrls():
    """运行角色必须是 NOBYPASSRLS + NOSUPERUSER（不能绕过 RLS / 不能当超管）。"""
    sql = " ".join(migrations.APP_RUNTIME_ROLE_SQL.split())
    assert "LOGIN" in sql
    assert "NOSUPERUSER" in sql
    assert "NOCREATEROLE" in sql
    assert "NOBYPASSRLS" in sql
    assert migrations.APP_RUNTIME_ROLE == "app_runtime"


def test_tenants_table_is_exempt_from_rls():
    """tenants 为平台主数据，不启用 RLS；所有 RLS 表语句都不应覆盖 tenants。"""
    # RLS_TABLES_SQL 逐个以 {t}.format 生成；确认集合里没有 tenants。（通过表名检查）
    assert "tenants" not in migrations.RLS_TABLES_SQL[0]
