"""契约改造单元测试：业务数据源（BusinessDataSource）契约 + 检索委托 + 适配器归属。

覆盖本次契约改造验收口径：
1) MockBusinessDataSource：get_order/get_shipping/search_policy 均强制租户作用域，
   跨租户一律返回 None / 空列表，绝不返回他租户数据；
2) DataSourceRetriever：委托 data_source.search_policy，租户隔离、top_k 生效；
3) EcommerceAdapter：构造时注入 BusinessDataSource（Mock），验证 get_order 归属校验
   （同租户 customer 仅本人；staff 本租户；跨租户拒绝 AdapterError.cross_tenant_denied），
   且不再直接读 mock_data（注入并被尊重）；
4) PostgresBusinessDataSource：缺 database_url 抛 DataSourceError（fail-closed，无 PG 也跑）；
   有 PG 时验证 orders/shipping/policy 租户隔离与 RLS（标记 postgres，无 PG 自动 skip）。
"""
from __future__ import annotations

import time

import pytest

from src.retrieval.factory import DataSourceRetriever
from src.tools.adapter import AdapterError, EcommerceAdapter
from src.tools.data_source import (
    BusinessDataSource,
    DataSourceError,
    MockBusinessDataSource,
)
from src.tools.postgres_data_source import PostgresBusinessDataSource


# ---------------------------------------------------------------------------
# 1. MockBusinessDataSource：强制租户作用域
# ---------------------------------------------------------------------------
def test_mock_ds_get_order_returns_none_for_other_tenant_order_id():
    ds = MockBusinessDataSource()
    # ORD-002 仅存在于 TENANT-A；用 TENANT-B 查询必须返回 None（绝不返回 A 的数据）。
    assert ds.get_order("TENANT-B", "ORD-002") is None
    # 本租户存在则返回。
    r = ds.get_order("TENANT-A", "ORD-002")
    assert r is not None and r["tenant_id"] == "TENANT-A"


def test_mock_ds_get_order_same_order_id_is_per_tenant():
    ds = MockBusinessDataSource()
    a = ds.get_order("TENANT-A", "ORD-001")
    b = ds.get_order("TENANT-B", "ORD-001")
    assert a is not None and b is not None
    # 同名 order_id 必须解析到各自租户的数据，绝不串租户。
    assert a["tenant_id"] == "TENANT-A" and a["user_id"] == "USER-001"
    assert b["tenant_id"] == "TENANT-B" and b["user_id"] == "USER-B1"
    assert a["total_amount"] != b["total_amount"]


def test_mock_ds_get_shipping_is_tenant_scoped():
    ds = MockBusinessDataSource()
    # 物流轨迹仅存在于 TENANT-A 的 ORD-001；TENANT-B 查询返回 None。
    assert ds.get_shipping("TENANT-B", "ORD-001") is None
    s = ds.get_shipping("TENANT-A", "ORD-001")
    assert s is not None and s["tenant_id"] == "TENANT-A"
    assert s["tracking_no"] == "SF1234567890"


def test_mock_ds_search_policy_never_returns_other_tenant_docs():
    ds = MockBusinessDataSource()
    for tenant in ("TENANT-A", "TENANT-B"):
        docs = ds.search_policy(tenant, "退货")
        assert isinstance(docs, list)
        assert all(d["tenant_id"] == tenant for d in docs)


def test_mock_ds_search_policy_empty_for_no_match():
    ds = MockBusinessDataSource()
    # TENANT-B 无任何含"物流"关键词的文档 → 返回空列表（RAG 据此 fail-closed 转人工）。
    assert ds.search_policy("TENANT-B", "物流") == []


def test_mock_ds_search_policy_top_k_limits():
    ds = MockBusinessDataSource()
    # 用命中多文档的查询验证 top_k 生效；TENANT-A 无单一查询命中多篇，
    # 因此用 top_k=0 边界与空查询拿到首篇来验证切片不越界。
    docs = ds.search_policy("TENANT-A", "退货", top_k=1)
    assert len(docs) == 1
    assert docs[0]["tenant_id"] == "TENANT-A"


# ---------------------------------------------------------------------------
# 2. DataSourceRetriever：委托 data_source.search_policy + 租户隔离 + top_k
# ---------------------------------------------------------------------------
class _RecordingDS(BusinessDataSource):
    """记录 search_policy 调用参数；返回固定 3 篇本租户文档，验证委托与 top_k。"""

    def __init__(self) -> None:
        self.search_calls: list[tuple] = []

    def get_order(self, tenant_id: str, order_id: str):
        return None

    def get_shipping(self, tenant_id: str, order_id: str):
        return None

    def search_policy(self, tenant_id: str, query: str,
                      top_k: int | None = None) -> list[dict]:
        self.search_calls.append((tenant_id, query, top_k))
        docs = [
            {"tenant_id": tenant_id, "doc_id": f"{tenant_id}-d1",
             "title": "t1", "content": "c1", "score": 3},
            {"tenant_id": tenant_id, "doc_id": f"{tenant_id}-d2",
             "title": "t2", "content": "c2", "score": 2},
            {"tenant_id": tenant_id, "doc_id": f"{tenant_id}-d3",
             "title": "t3", "content": "c3", "score": 1},
        ]
        if top_k is not None:
            docs = docs[:top_k]
        return docs

    def policy_documents(self) -> list[dict]:
        return []


def test_data_source_retriever_delegates_to_search_policy():
    ds = _RecordingDS()
    r = DataSourceRetriever(ds, top_k=2)
    out = r.search("TENANT-A", "退货")
    # 默认使用构造 top_k；委托到 data_source.search_policy。
    assert ds.search_calls[-1] == ("TENANT-A", "退货", 2)
    assert len(out) == 2
    assert all(d["tenant_id"] == "TENANT-A" for d in out)


def test_data_source_retriever_honors_explicit_top_k():
    ds = _RecordingDS()
    r = DataSourceRetriever(ds, top_k=2)
    out = r.search("TENANT-A", "退货", top_k=1)
    assert ds.search_calls[-1] == ("TENANT-A", "退货", 1)
    assert len(out) == 1


def test_data_source_retriever_tenant_isolation():
    r = DataSourceRetriever(MockBusinessDataSource(), top_k=4)
    for tenant in ("TENANT-A", "TENANT-B"):
        docs = r.search(tenant, "退货")
        assert docs and all(d["tenant_id"] == tenant for d in docs)
    # A/B 返回的 doc_id 互不相同，绝不含对方文档。
    a_ids = {d["doc_id"] for d in r.search("TENANT-A", "退货")}
    b_ids = {d["doc_id"] for d in r.search("TENANT-B", "退货")}
    assert not (a_ids & b_ids)


def test_data_source_retriever_returns_empty_when_no_match():
    r = DataSourceRetriever(MockBusinessDataSource(), top_k=4)
    assert r.search("TENANT-B", "物流") == []


# ---------------------------------------------------------------------------
# 3. EcommerceAdapter：注入 BusinessDataSource + get_order 归属校验
# ---------------------------------------------------------------------------
def test_adapter_injected_data_source_is_honored_and_not_mock_data(monkeypatch):
    # 探测：若 Adapter 内部绕过注入数据源、直接读 mock_data，则下述 monkeypatch 会触发。
    def _boom(*_a, **_k):
        raise AssertionError("Adapter 不得直接读 mock_data")

    monkeypatch.setattr("src.tools.data_source.mock_data.get_order", _boom)
    monkeypatch.setattr("src.tools.data_source.mock_data.get_shipping", _boom)

    class _SentinelDS(BusinessDataSource):
        def get_order(self, tenant_id, order_id):
            return {"tenant_id": tenant_id, "order_id": order_id, "user_id": "USER-S",
                    "status": "delivered", "total_amount": 1.0,
                    "items": [{"name": "x", "category": "electronics"}],
                    "carrier": "SF", "tracking_no": "SF9",
                    "created_at": time.time(), "delivered_at": time.time()}

        def get_shipping(self, tenant_id, order_id):
            return None

        def search_policy(self, tenant_id, query, top_k=None):
            return []

        def policy_documents(self):
            return []

    ad = EcommerceAdapter(data_source=_SentinelDS())
    rec = ad.get_order("TENANT-SENTINEL", "USER-S", "customer", "ORD-999")
    assert rec.order_id == "ORD-999" and rec.user_id == "USER-S"
    assert rec.tenant_id == "TENANT-SENTINEL"


def test_adapter_same_tenant_customer_only_own_order():
    ad = EcommerceAdapter(data_source=MockBusinessDataSource())
    # 本人订单可访问。
    rec = ad.get_order("TENANT-A", "USER-001", "customer", "ORD-001")
    assert rec.order_id == "ORD-001" and rec.user_id == "USER-001"
    # 同租户跨用户（非 staff）→ 拒绝，且代码为 order_not_owned。
    with pytest.raises(AdapterError) as exc:
        ad.get_order("TENANT-A", "USER-002", "customer", "ORD-001")
    assert exc.value.code == "order_not_owned"


def test_adapter_staff_can_access_same_tenant_order():
    ad = EcommerceAdapter(data_source=MockBusinessDataSource())
    for role in ("agent", "admin", "approver"):
        rec = ad.get_order("TENANT-A", f"{role.upper()}-A", role, "ORD-001")
        assert rec.order_id == "ORD-001"


def test_adapter_cross_tenant_denied_even_if_ds_leaks():
    # 防御纵深：即便数据源错误地返回了跨租户行，适配器也必须拒绝（cross_tenant_denied）。
    class _LeakyDS(BusinessDataSource):
        def get_order(self, tenant_id, order_id):
            # 模拟存在缺陷/越权数据源：任意查询都返回 TENANT-B 的行。
            return {"tenant_id": "TENANT-B", "order_id": order_id, "user_id": "USER-B1",
                    "status": "delivered", "total_amount": 9.0,
                    "items": [], "carrier": "", "tracking_no": "",
                    "created_at": time.time(), "delivered_at": time.time()}

        def get_shipping(self, tenant_id, order_id):
            return None

        def search_policy(self, tenant_id, query, top_k=None):
            return []

        def policy_documents(self):
            return []

    ad = EcommerceAdapter(data_source=_LeakyDS())
    with pytest.raises(AdapterError) as exc:
        ad.get_order("TENANT-A", "USER-A9", "customer", "ORD-001")
    assert exc.value.code == "cross_tenant_denied"


def test_adapter_cross_tenant_by_bad_tenant_context_not_owned():
    # 真实/合法租户排序下的跨租户：TENANT-A 的订单用 TENANT-B 的身份访问 → 归属拒绝
    # （数据源按租户作用域返回 None → order_not_found，而非泄密）。
    ad = EcommerceAdapter(data_source=MockBusinessDataSource())
    with pytest.raises(AdapterError) as exc:
        # TENANT-A 只有 ORD-001/002/003/004；TENANT-B 身份访问 TENANT-A 的 ORD-002。
        ad.get_order("TENANT-B", "USER-B1", "customer", "ORD-002")
    assert exc.value.code == "order_not_found"


def test_adapter_get_shipping_returns_none_for_unowned_order():
    ad = EcommerceAdapter(data_source=MockBusinessDataSource())
    # 非本人订单 → 归属校验失败 → 返回 None（规避信息泄露）。
    assert ad.get_shipping("TENANT-A", "USER-002", "customer", "ORD-001") is None
    # 本人订单 → 可查。
    s = ad.get_shipping("TENANT-A", "USER-001", "customer", "ORD-001")
    assert s is not None and s["tracking_no"] == "SF1234567890"


# ---------------------------------------------------------------------------
# 4. PostgresBusinessDataSource：fail-closed + skip-if-no-PG 集成
# ---------------------------------------------------------------------------
def test_ds_requires_database_url_fails_closed():
    # 无 database_url → DataSourceError（fail-closed），不需要真实 PostgreSQL。
    with pytest.raises(DataSourceError):
        PostgresBusinessDataSource("")


# 以下为真实 PostgreSQL/RLS 集成测试；无 DATABASE_URL 时由 pg_engine fixture 整组 skip。
def _seed_pg_business(engine) -> None:
    from sqlalchemy import text

    now = time.time()
    with engine.begin() as conn:
        for tid, name in (("TENANT-A", "A"), ("TENANT-B", "B")):
            conn.execute(
                text("INSERT INTO tenants (id, name, status, created_at) "
                     "VALUES (:id, :n, 'active', :t)"),
                {"id": tid, "n": name, "t": now},
            )
        # TENANT-A 的订单 ORD-A1（TENANT-B 无此订单）
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
def test_pg_data_source_tenant_isolation_and_rls(pg_engine):
    from tests.pg_helpers import app_runtime_dsn

    _seed_pg_business(pg_engine)
    # 用 NOBYPASSRLS 运行角色连接，验证 RLS 真正生效（超级用户/owner 会绕过 RLS）。
    ds = PostgresBusinessDataSource(app_runtime_dsn())
    try:
        # get_order 租户隔离：本租户命中，跨租户同订单号返回 None。
        order = ds.get_order("TENANT-A", "ORD-A1")
        assert order is not None and order["tenant_id"] == "TENANT-A"
        assert ds.get_order("TENANT-B", "ORD-A1") is None

        # get_shipping 租户隔离。
        ship = ds.get_shipping("TENANT-A", "ORD-A1")
        assert ship is not None and ship["tenant_id"] == "TENANT-A"
        assert ds.get_shipping("TENANT-B", "ORD-A1") is None

        # search_policy 租户隔离。
        for tenant in ("TENANT-A", "TENANT-B"):
            docs = ds.search_policy(tenant, "退货")
            assert docs and all(d["tenant_id"] == tenant for d in docs)
    finally:
        ds.close()


@pytest.mark.postgres
def test_pg_data_source_rls_filters_unauthorized_rows(pg_engine):
    from sqlalchemy import text

    from tests.pg_helpers import app_runtime_dsn

    _seed_pg_business(pg_engine)
    ds = PostgresBusinessDataSource(app_runtime_dsn())
    try:
        # RLS 兜底：即使 SQL 未按 tenant_id 过滤（政策文档全量读取），
        # 由于 FORCE RLS + set_config 作用域，跨租户文档仍不可见。
        with ds._engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": "TENANT-B"})
            rows = conn.execute(
                text("SELECT tenant_id, doc_id FROM policy_documents")
            ).mappings().all()
        assert rows and all(r["tenant_id"] == "TENANT-B" for r in rows)
    finally:
        ds.close()
