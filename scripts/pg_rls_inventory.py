"""采集 PostgreSQL 数据面 RLS 策略清单 + 角色 / 所有权检查结果（Preview 服务器验收用）。

用超级用户连接只做只读元数据查询（不建表/不改对象），输出 evidence/pg_rls_policies.json。
环境变量：DATABASE_URL（超级用户连接，如 postgres/postgres@127.0.0.1:55432/acceptance）
"""
from __future__ import annotations

import json
import os
import time

from sqlalchemy import text, create_engine

from src.infrastructure.postgres_store import _to_sqlalchemy_url


def main() -> None:
    eng = create_engine(_to_sqlalchemy_url(os.environ["DATABASE_URL"]), pool_pre_ping=True)
    out = {}

    with eng.connect() as conn:
        # 1) RLS 启用/强制的表（tenants 应为 false，其余应为 true）
        rls = conn.execute(text(
            "SELECT relname, relrowsecurity AS rls_enabled, relforcerowsecurity AS rls_forced "
            "FROM pg_class WHERE relkind='r' "
            "AND relname IN ('tenants','memberships','sessions','operations','approvals','executions',"
            "'streams','stream_events','audit','checkpoint_thread_scopes','checkpoints',"
            "'checkpoint_blobs','checkpoint_writes') ORDER BY relname"
        )).mappings().all()
        out["rls_enabled_tables"] = [dict(r) for r in rls]

        # 2) 策略清单
        pols = conn.execute(text(
            "SELECT tablename, policyname, permissive, roles, cmd, qual, with_check "
            "FROM pg_policies ORDER BY tablename, policyname"
        )).mappings().all()
        out["policies"] = [dict(r) for r in pols]

        # 3) 角色标志（app_runtime 必须 NONBYpassRLS + NOSUPERUSER）
        roles = conn.execute(text(
            "SELECT rolname, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole "
            "FROM pg_roles WHERE rolname IN ('postgres','migrator','app_runtime') ORDER BY rolname"
        )).mappings().all()
        out["roles"] = [dict(r) for r in roles]

        # 4) 作用域函数
        funcs = conn.execute(text(
            "SELECT p.proname, pg_get_function_identity_arguments(p.oid) AS args "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='public' AND p.proname IN ('app_current_tenant_id','checkpoint_thread_in_current_tenant')"
        )).mappings().all()
        out["scope_functions"] = [dict(r) for r in funcs]

        # 5) 表 owner（迁移角色）；运行角色不得为以下表 owner
        owners = conn.execute(text(
            "SELECT tablename, tableowner FROM pg_tables "
            "WHERE schemaname='public' AND tablename IN "
            "('sessions','operations','approvals','executions','checkpoint_thread_scopes',"
            "'checkpoints','checkpoint_blobs','checkpoint_writes') ORDER BY tablename"
        )).mappings().all()
        out["table_owners"] = [dict(r) for r in owners]

        # 6) app_runtime 对业务表的直接 DML 权限（已被 GRANT；owner 应为迁移角色）
        privs = conn.execute(text(
            "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
            "WHERE grantee='app_runtime' AND table_schema='public' "
            "AND table_name IN ('sessions','operations','approvals','executions') ORDER BY table_name"
        )).mappings().all()
        out["app_runtime_dml_grants"] = [dict(r) for r in privs]

    eng.dispose()

    out["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    out["pg_server"] = os.environ["DATABASE_URL"].split("@")[-1]
    # 断言：除 tenants 外业务表/checkpoint 表全部 ENABLE+FORCE RLS；app_runtime 非超级/非 bypassrls。
    rls_map = {r["relname"]: (r["rls_enabled"], r["rls_forced"]) for r in out["rls_enabled_tables"]}
    non_tenant = [t for t in rls_map if t != "tenants"]
    all_rls_non_tenant = all(rls_map[t][0] and rls_map[t][1] for t in non_tenant)
    app = next((r for r in out["roles"] if r["rolname"] == "app_runtime"), None)
    app_ok = bool(app) and (not app["rolsuper"]) and (not app["rolbypassrls"])
    out["checks"] = {
        "all_non_tenant_tables_rls_enabled_and_forced": all_rls_non_tenant,
        "tenants_not_rls": (not rls_map.get("tenants", (False, False))[0]),
        "app_runtime_not_superuser_and_not_bypassrls": app_ok,
        "scope_functions_present": len(out["scope_functions"]) == 2,
    }
    out["conclusion"] = all(out["checks"].values())

    with open("evidence/pg_rls_policies.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
