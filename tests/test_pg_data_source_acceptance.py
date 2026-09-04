"""PostgreSQL / RLS 业务数据源验收（orders / shipping_events / policy_documents）。

对应《生产基线与验收测试》§4 与 evidence/PG_RLS_ACCEPTANCE_PROCEDURE.md：验证"业务读路径经受控
真实系统"在 PostgreSQL/RLS 数据面上真正生效。三条验收断言：

1. 同租户可读：本租户作用域内订单/物流/政策命中；
2. 跨租户零可见：其它租户上下文读同订单号返回 None（含 RLS 兜底），政策文档仅本租户命中；
3. 未设置 app.tenant_id 时零可见：RLS 无作用域兜底，三张业务表零可见。

仅当存在真实 PostgreSQL 时运行：pg_engine fixture 在无 DATABASE_URL / 连接失败时整组 skip。
本环境（无 Docker / PostgreSQL）该组如实 skip，不代表通过；具备 DB 后才能得到可复核证据。
"""
from __future__ import annotations

import time

import pytest
from sqlalchemy import text

from src.tools.postgres_data_source import PostgresBusinessDataSource
from tests.pg_helpers import app_runtime_dsn


def _seed_two_tenants_business(pg_engine) -> None:
    """用超级用户 engine（绕过 RLS）预置两租户订单/物流/政策。

    数据源/适配器归属校验（customer 仅本人 / staff 本租户）不在此处；本文件只验证
    租户作用域隔离（同租户可读 / 跨租户零可见 / 无作用域零可见）。
    """
    now = time.time()
    with pg_engine.begin() as conn:
        for tid, name in (("TENANT-A", "A"), ("TENANT-B", "B")):
            conn.execute(
                text("INSERT INTO tenants (id, name, status, created_at) "
                     "VALUES (:id, :n, 'active', :t)"),
                {"id": tid, "n": name, "t": now},
            )
        # 订单 ORD-A1 仅属于 TENANT-A；TENANT-B 无此订单。
        conn.execute(
            text("INSERT INTO orders (tenant_id, order_id, user_id, status, total_amount, "
                 "items, carrier, tracking_no, created_at, delivered_at) "
                 "VALUES (:t, :o, :u, :s, :a, :i, :c, :tr, :ca, :d)"),
            {"t": "TENANT-A", "o": "ORD-A1", "u": "USER-A", "s": "delivered",
             "a": 100.0, "i": '[{"name":"x","category":"electronics"}]',
             "c": "SF", "tr": "SF1", "ca": now, "d": now},
        )
        conn.execute(
            text("INSERT INTO shipping_events (tenant_id, order_id, tracking_no, events) "
                 "VALUES (:t, :o, :tr, :e)"),
            {"t": "TENANT-A", "o": "ORD-A1", "tr": "SF1", "e": '[{"status":"已签收"}]'},
        )
        conn.execute(
            text("INSERT INTO policy_documents (tenant_id, doc_id, title, content) "
                 "VALUES ('TENANT-A', 'ret_A', '退货政策A', '七天无理由退货')"),
        )
        conn.execute(
            text("INSERT INTO policy_documents (tenant_id, doc_id, title, content) "
                 "VALUES ('TENANT-B', 'ret_B', '退货政策B', '三十天无理由退货')"),
        )


@pytest.mark.postgres
def test_pg_business_ds_acceptance_same_tenant_readable(pg_engine):
    _seed_two_tenants_business(pg_engine)
    # 用 NOBYPASSRLS 运行角色连接（超级用户/owner 会绕过 RLS，无法验证隔离）。
    ds = PostgresBusinessDataSource(app_runtime_dsn())
    try:
        order = ds.get_order("TENANT-A", "ORD-A1")
        assert order is not None
        assert order["tenant_id"] == "TENANT-A" and order["order_id"] == "ORD-A1"
        ship = ds.get_shipping("TENANT-A", "ORD-A1")
        assert ship is not None and ship["tenant_id"] == "TENANT-A"
        docs = ds.search_policy("TENANT-A", "退货")
        assert docs and all(d["tenant_id"] == "TENANT-A" for d in docs)
    finally:
        ds.close()


@pytest.mark.postgres
def test_pg_business_ds_acceptance_cross_tenant_zero_visible(pg_engine):
    _seed_two_tenants_business(pg_engine)
    ds = PostgresBusinessDataSource(app_runtime_dsn())
    try:
        # 跨租户：B 上下文读 A 的订单号 → None（不区分"不存在/不属于本租户"，避免泄露）。
        assert ds.get_order("TENANT-B", "ORD-A1") is None
        assert ds.get_shipping("TENANT-B", "ORD-A1") is None
        # 政策：仅本租户文档命中，绝不混入其它租户。
        for tenant in ("TENANT-A", "TENANT-B"):
            docs = ds.search_policy(tenant, "退货")
            assert docs and all(d["tenant_id"] == tenant for d in docs)
    finally:
        ds.close()


@pytest.mark.postgres
def test_pg_business_ds_acceptance_unset_tenant_zero_visible(pg_engine):
    _seed_two_tenants_business(pg_engine)
    ds = PostgresBusinessDataSource(app_runtime_dsn())
    try:
        # 未设置 app.tenant_id → app_current_tenant_id() 返回 NULL → RLS 作用域为空 → 三表零可见。
        with ds._engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', NULL, true)"))
            assert conn.execute(text("SELECT count(*) FROM orders")).scalar() == 0
            assert conn.execute(text("SELECT count(*) FROM shipping_events")).scalar() == 0
            assert conn.execute(text("SELECT count(*) FROM policy_documents")).scalar() == 0
    finally:
        ds.close()
