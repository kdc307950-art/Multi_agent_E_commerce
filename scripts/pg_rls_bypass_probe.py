"""PostgreSQL RLS 绕过探测（真实数据库层验证，对应 t4 子项 2）。

与 `scripts/pg_rls_inventory.py`（只读元数据清单）互补：本脚本在真实 PG 上**动态注入绕过**
并断言 RLS 确实拦截。追加证据到 `evidence/pg_rls_bypass_probe.json`。

必须有可连接的 PostgreSQL（环境变量 DATABASE_URL，建议超管连接用于建 schema/角色），
且 `tests/pg_helpers.py` 的 `setup_runtime_role` 会被调用以创建 NOBYPASSRLS 的 app_runtime。
本机无 PG 时脚本退出并明确提示（不伪造 through）。

覆盖（与 tests/test_rls_bypass.py 对齐）：
  B1 跨租户直连 SQL 查询（显式 WHERE 其它租户）→ 零可见
  B2 跨租户直连 SQL 写入 → WITH CHECK 拒绝
  B3 绕过应用层直接改 tenant_id → WITH CHECK 拒绝
  B4 备份/超管连接天然绕过 RLS（PostgreSQL 语义）；运行角色 NOBYPASSRLS 且非 owner 无法越权
  B5 app_runtime 无法绕过 RLS（rolbypassrls=false + FORCE RLS 生效 + 跨租户查询零可见）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402

from tests.pg_helpers import (  # noqa: E402
    app_runtime_dsn,
    make_pg_engine,
    reset_pg_schema,
    setup_runtime_role,
)
from src.infrastructure.postgres_store import _to_sqlalchemy_url  # noqa: E402
from src.core.types import Role  # noqa: E402
from src.infrastructure.postgres_store import PostgresStore  # noqa: E402


def _set_tenant(conn, tenant_id):
    conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})


def _count(conn, sql, **params) -> int:
    return int(conn.execute(text(sql), params).scalar())


def _seed(engine, tenant_id, thread_id="th"):
    store = PostgresStore(engine.url.render_as_string(hide_password=False), engine=engine)
    try:
        store.create_tenant(tenant_id, f"租户-{tenant_id}")
        store.add_membership(tenant_id, f"USER-{tenant_id}", Role.CUSTOMER)
        store.create_session(tenant_id, f"USER-{tenant_id}", thread_id, 1000.0, 7)
    finally:
        store.close()


def main() -> None:
    ap = argparse.ArgumentParser(
        description="PostgreSQL RLS 绕过动态探测（真实 DB）。连接由 DATABASE_URL 或 --database-url 提供。")
    ap.add_argument("--database-url", default=None,
                    help="PostgreSQL 超管/迁移角色连接串（覆盖 DATABASE_URL）；"
                         "运行角色凭据另由 APP_RUNTIME_USER / APP_RUNTIME_PASSWORD 提供。"
                         "容器网络内主机名即服务名 postgres，端口 5432。")
    ap.add_argument("--out", default="evidence/pg_rls_bypass_probe.json")
    args = ap.parse_args()

    if args.database_url:
        os.environ["DATABASE_URL"] = args.database_url
    if not os.environ.get("DATABASE_URL"):
        print("PostgreSQL 不可用：设置 DATABASE_URL 或 --database-url；本次跳过，不伪造证据。")
        sys.exit(0)

    super_eng = make_pg_engine()
    if super_eng is None:
        print("PostgreSQL 不可用：连接失败；本次跳过，不伪造证据。")
        sys.exit(0)
    # ⚠ `reset_pg_schema`/`setup_runtime_role` 为破坏性 DDL，只能对专用测试库执行，
    #   禁止指向共享 preview `langgraph` 库（避免清空/干扰集群数据，符合 captain 分库隔离约束）。
    reset_pg_schema(super_eng)
    setup_runtime_role(super_eng)

    app_eng = create_engine(_to_sqlalchemy_url(app_runtime_dsn()), pool_pre_ping=True)
    _seed(super_eng, "TENANT-A", "th-a")
    _seed(super_eng, "TENANT-B", "th-b")

    out: dict = {}

    # B1 跨租户直连查询零可见
    with app_eng.begin() as conn:
        _set_tenant(conn, "TENANT-A")
        out["b1_cross_tenant_query_B_rows_visible"] = _count(
            conn, "SELECT count(*) FROM sessions WHERE tenant_id='TENANT-B'")
        out["b1_own_tenant_rows_visible"] = _count(
            conn, "SELECT count(*) FROM sessions WHERE tenant_id='TENANT-A'")

    # B2 跨租户直连写入被拒
    b2_rejected = False
    with app_eng.begin() as conn:
        _set_tenant(conn, "TENANT-B")
        try:
            conn.execute(text(
                "INSERT INTO sessions(tenant_id,thread_id,user_id,created_at,expires_at,last_active_at,status) "
                "VALUES('TENANT-A','th-x','u',1,1,1,'active')"))
        except Exception:
            b2_rejected = True
    out["b2_cross_tenant_direct_write_rejected"] = b2_rejected

    # B3 直接改 tenant_id 被拒
    b3_rejected = False
    with app_eng.begin() as conn:
        _set_tenant(conn, "TENANT-A")
        try:
            conn.execute(text(
                "UPDATE sessions SET tenant_id='TENANT-B' "
                "WHERE tenant_id='TENANT-A' AND thread_id='th-a'"))
        except Exception:
            b3_rejected = True
    # PG 在 WITH CHECK 拒绝后当前事务已 aborted，故用新事务读回验证原行未变更（跨租户零污染）。
    with app_eng.begin() as conn:
        _set_tenant(conn, "TENANT-A")
        out["b3_original_row_intact"] = _count(
            conn, "SELECT count(*) FROM sessions WHERE tenant_id='TENANT-A'")
        out["b3_cross_tenant_still_zero"] = _count(
            conn, "SELECT count(*) FROM sessions WHERE tenant_id='TENANT-B'")
    out["b3_direct_update_tenant_id_rejected"] = b3_rejected

    # B4 备份/超管连接：PostgreSQL 语义上 superuser 天然绕过 RLS；运行角色 NOBYPASSRLS 非 owner。
    out["b4_runtime_role_flags"] = {}
    with super_eng.connect() as conn:
        row = conn.execute(text(
            "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname='app_runtime'"
        )).mappings().first()
        out["b4_runtime_role_flags"] = dict(row) if row else None

    # B5 app_runtime 无法绕过：NOBYPASSRLS + FORCE RLS + 跨租户零可见 + 未设作用域零可见
    with super_eng.connect() as conn:
        rls = conn.execute(text(
            "SELECT relrowsecurity AS rls_enabled, relforcerowsecurity AS rls_forced "
            "FROM pg_class WHERE relname='sessions'")).mappings().first()
        out["b5_sessions_rls"] = dict(rls) if rls else None
    with app_eng.begin() as conn:
        _set_tenant(conn, None)
        out["b5_no_tenant_scope_zero_visible"] = _count(conn, "SELECT count(*) FROM sessions")
    with app_eng.begin() as conn:
        _set_tenant(conn, "TENANT-A")
        out["b5_cross_tenant_B_zero_visible"] = _count(
            conn, "SELECT count(*) FROM sessions WHERE tenant_id='TENANT-B'")
        out["b5_own_tenant_A_visible"] = _count(
            conn, "SELECT count(*) FROM sessions WHERE tenant_id='TENANT-A'")

    out["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    runtime_flags = out.get("b4_runtime_role_flags") or {}
    out["conclusion"] = all([
        out["b1_cross_tenant_query_B_rows_visible"] == 0 and out["b1_own_tenant_rows_visible"] >= 1,
        out["b2_cross_tenant_direct_write_rejected"] is True,
        out["b3_direct_update_tenant_id_rejected"] is True and out["b3_original_row_intact"] == 1,
        runtime_flags.get("rolbypassrls") is False and runtime_flags.get("rolsuper") is False,
        (out.get("b5_sessions_rls") or {}).get("rls_enabled") is True
        and (out.get("b5_sessions_rls") or {}).get("rls_forced") is True,
        out["b5_no_tenant_scope_zero_visible"] == 0,
        out["b5_cross_tenant_B_zero_visible"] == 0 and out["b5_own_tenant_A_visible"] >= 1,
    ])

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    app_eng.dispose()
    super_eng.dispose()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print("PG_RLS_BYPASS_PROBE conclusion =", out["conclusion"])


if __name__ == "__main__":
    main()
