"""执行引擎验收测试：shadow/live、幂等、回调验签与重放、补偿、对账、归属校验。

覆盖本次验收口径：
- 真实/沙箱订单归属校验通过：customer 只能执行本人订单，staff 可本租户，跨租户拒绝；
- 重复请求/重复审批/回调重放不产生重复扣款/退款（同一 operation → 同一执行记录；
  回调同 nonce 只重放不重复生效，异 nonce 冲突转人工）；
- 异常操作转人工并可对账（失败补偿 + 对账收敛 mismatch → human_handoff）；
- shadow 第一轮只生成待执行记录 + 模拟回执，不触真实资金；live 受控执行开关。
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from src.auth.security import issue_token
from src.core.types import (
    OperationStatus,
    PendingAction,
    Role,
    generate_operation_key,
)
from src.execution import (
    ExecutionEngine,
    ExecutionMode,
    ExecutionStatus,
    MockFundsProvider,
    build_callback_signature,
)
from src.execution.provider import ProviderError
from src.infrastructure.store import MemoryStore
from src.llm.mock import MockLLM
from src.main import create_app
from src.tools import EcommerceAdapter, build_adapter
from tests.conftest import bearer


DEFAULT_CALLBACK_SECRET = "shadow-callback-secret"


def _seed(store: MemoryStore) -> None:
    store.create_tenant("TENANT-A", "a")
    store.create_tenant("TENANT-B", "b")
    store.add_membership("TENANT-A", "USER-001", Role.CUSTOMER)
    store.add_membership("TENANT-A", "USER-002", Role.CUSTOMER)
    store.add_membership("TENANT-A", "ADMIN-A", Role.ADMIN)
    store.add_membership("TENANT-B", "USER-B1", Role.CUSTOMER)


def _op(store: MemoryStore, action: PendingAction, order: str, rid: str, tenant="TENANT-A",
        thread="th"):
    key = generate_operation_key(action, tenant, order, rid)
    return store.create_operation(tenant, thread, order, action, key, time.time())


# ---------------------------------------------------------------------------
# shadow：只生成待执行记录 + 模拟回执，不触真实资金
# ---------------------------------------------------------------------------
def test_shadow_execute_creates_record_and_simulated_receipt():
    s = MemoryStore(); _seed(s)
    ad = EcommerceAdapter(execution_mode=ExecutionMode.SHADOW)  # provider=None → 不触真实接口
    op = _op(s, PendingAction.REFUND, "ORD-001", "REQ-SH1")
    out = ad.execute_operation(s, "TENANT-A", "USER-001", "customer", op.operation_id)
    assert out.status is ExecutionStatus.CONFIRMED
    assert out.mode is ExecutionMode.SHADOW
    assert out.receipt["simulated"] is True  # 模拟回执标记
    rec = s.get_execution_record("TENANT-A", out.execution_id)
    assert rec.status is ExecutionStatus.CONFIRMED
    assert rec.receipt["simulated"] is True
    assert s.get_operation("TENANT-A", op.operation_id).status == OperationStatus.EXECUTED


def test_shadow_execution_is_idempotent():
    s = MemoryStore(); _seed(s)
    ad = EcommerceAdapter(execution_mode=ExecutionMode.SHADOW)
    op = _op(s, PendingAction.REFUND, "ORD-001", "REQ-SH2")
    first = ad.execute_operation(s, "TENANT-A", "USER-001", "customer", op.operation_id)
    second = ad.execute_operation(s, "TENANT-A", "USER-001", "customer", op.operation_id)
    # 同一 operation → 同一 execution_id（幂等重放，不重复提交外部）。
    assert second.execution_id == first.execution_id
    assert "幂等重放" in second.message
    assert len(s.list_execution_records("TENANT-A")) == 1


def test_shadow_address_change_execute_confirmed():
    s = MemoryStore(); _seed(s)
    ad = EcommerceAdapter(execution_mode=ExecutionMode.SHADOW)
    op = _op(s, PendingAction.RETURN_ADDRESS, "ORD-001", "REQ-SH3")
    out = ad.execute_operation(s, "TENANT-A", "USER-001", "customer", op.operation_id)
    assert out.status is ExecutionStatus.CONFIRMED
    assert s.get_operation("TENANT-A", op.operation_id).status == OperationStatus.EXECUTED


# ---------------------------------------------------------------------------
# 归属校验：customer 仅本人订单；staff 本租户；跨租户拒绝
# ---------------------------------------------------------------------------
def test_execution_requires_order_ownership():
    s = MemoryStore(); _seed(s)
    ad = EcommerceAdapter(execution_mode=ExecutionMode.SHADOW)
    op = _op(s, PendingAction.REFUND, "ORD-001", "REQ-OWN1")
    # USER-002（同租户非归属、非 staff）执行 USER-001 的订单 → 拒绝。
    with pytest.raises(Exception):
        ad.execute_operation(s, "TENANT-A", "USER-002", "customer", op.operation_id)
    # staff（admin）可本租户执行。
    ok = ad.execute_operation(s, "TENANT-A", "ADMIN-A", "admin", op.operation_id)
    assert ok.status is ExecutionStatus.CONFIRMED
    # 跨租户：TENANT-B 的 customer 用 TENANT-B 上下文执行 TENANT-A 操作 → 操作 404。
    with pytest.raises(Exception):
        ad.execute_operation(s, "TENANT-B", "USER-B1", "customer", op.operation_id)


# ---------------------------------------------------------------------------
# live：受控执行开关；需 provider；提交后回调确认；回调重放不重复
# ---------------------------------------------------------------------------
def test_live_execution_requires_provider():
    s = MemoryStore(); _seed(s)
    # live 但无 provider → fail-closed（受控执行开关未开）。
    ad = EcommerceAdapter(execution_mode=ExecutionMode.LIVE, provider=None)
    op = _op(s, PendingAction.REFUND, "ORD-001", "REQ-L1")
    with pytest.raises(Exception):
        ad.execute_operation(s, "TENANT-A", "USER-001", "customer", op.operation_id)


def test_live_submit_then_callback_confirm_replay_safe():
    s = MemoryStore(); _seed(s)
    provider = MockFundsProvider(result="success")
    ad = EcommerceAdapter(execution_mode=ExecutionMode.LIVE, provider=provider,
                          callback_secret=DEFAULT_CALLBACK_SECRET)
    op = _op(s, PendingAction.REFUND, "ORD-001", "REQ-L2")
    out = ad.execute_operation(s, "TENANT-A", "USER-001", "customer", op.operation_id)
    assert out.status is ExecutionStatus.SUBMITTED
    rec = s.get_execution_record("TENANT-A", out.execution_id)
    assert rec.external_txn_id  # 提交到外部获得外部队列号

    payload = {"tenant_id": "TENANT-A", "execution_id": rec.execution_id,
               "external_txn_id": rec.external_txn_id, "status": "succeeded",
               "amount": rec.amount, "nonce": "nonce-L2"}
    sig = build_callback_signature(DEFAULT_CALLBACK_SECRET, payload)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    engine = ad.make_execution_engine(s)
    r1 = engine.apply_callback(body, sig)
    assert r1.applied is True and r1.status == ExecutionStatus.CONFIRMED.value
    assert s.get_operation("TENANT-A", op.operation_id).status == OperationStatus.EXECUTED

    # 重放同 nonce → 只重放不重复生效。
    r2 = engine.apply_callback(body, sig)
    assert r2.applied is False and r2.reason == "replay"
    assert s.get_execution_record("TENANT-A", rec.execution_id).status is ExecutionStatus.CONFIRMED
    assert len(s.list_execution_records("TENANT-A")) == 1  # 未新增执行/重复扣款


def test_callback_invalid_signature_rejected():
    s = MemoryStore(); _seed(s)
    provider = MockFundsProvider(result="success")
    ad = EcommerceAdapter(execution_mode=ExecutionMode.LIVE, provider=provider,
                          callback_secret=DEFAULT_CALLBACK_SECRET)
    op = _op(s, PendingAction.REFUND, "ORD-001", "REQ-L3")
    out = ad.execute_operation(s, "TENANT-A", "USER-001", "customer", op.operation_id)
    engine = ad.make_execution_engine(s)
    payload = {"tenant_id": "TENANT-A", "execution_id": out.execution_id, "status": "succeeded",
               "amount": 299.0, "nonce": "n-bad"}
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    r = engine.apply_callback(body, "deadbeef")  # 坏签名
    assert r.reason == "signature_invalid" and r.applied is False
    assert s.get_execution_record("TENANT-A", out.execution_id).status is ExecutionStatus.SUBMITTED


def test_callback_amount_mismatch_goes_human():
    s = MemoryStore(); _seed(s)
    provider = MockFundsProvider(result="success")
    ad = EcommerceAdapter(execution_mode=ExecutionMode.LIVE, provider=provider,
                          callback_secret=DEFAULT_CALLBACK_SECRET)
    op = _op(s, PendingAction.REFUND, "ORD-001", "REQ-L4")
    out = ad.execute_operation(s, "TENANT-A", "USER-001", "customer", op.operation_id)
    engine = ad.make_execution_engine(s)
    rec = s.get_execution_record("TENANT-A", out.execution_id)
    # 回调金额与执行记录不一致 → 冲突 → 转人工对账。
    p = {"tenant_id": "TENANT-A", "execution_id": rec.execution_id, "status": "succeeded",
         "amount": 1.0, "nonce": "nonce-amt"}
    body = json.dumps(p, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    r = engine.apply_callback(body, build_callback_signature(DEFAULT_CALLBACK_SECRET, p))
    assert r.applied is False and r.reason == "amount_mismatch"
    assert s.get_execution_record("TENANT-A", rec.execution_id).status is ExecutionStatus.MISMATCHED
    assert s.get_operation("TENANT-A", op.operation_id).status == OperationStatus.HUMAN_HANDOFF


# ---------------------------------------------------------------------------
# 失败补偿：live 外部明确失败 → 补偿（回滚）
# ---------------------------------------------------------------------------
def test_live_failure_compensated():
    s = MemoryStore(); _seed(s)
    provider = MockFundsProvider(result="failure")
    ad = EcommerceAdapter(execution_mode=ExecutionMode.LIVE, provider=provider,
                          callback_secret=DEFAULT_CALLBACK_SECRET)
    op = _op(s, PendingAction.REFUND, "ORD-001", "REQ-F1")
    out = ad.execute_operation(s, "TENANT-A", "USER-001", "customer", op.operation_id)
    assert out.status is ExecutionStatus.COMPENSATED
    rec = s.get_execution_record("TENANT-A", out.execution_id)
    assert rec.compensation_status == "compensated"
    assert s.get_operation("TENANT-A", op.operation_id).status == OperationStatus.EXECUTED


# ---------------------------------------------------------------------------
# 对账：SUBMITTED 但未回调 → 查询外部并收口；不一致转人工
# ---------------------------------------------------------------------------
def test_reconcile_confirms_submitted_success():
    s = MemoryStore(); _seed(s)
    provider = MockFundsProvider(result="success")
    ad = EcommerceAdapter(execution_mode=ExecutionMode.LIVE, provider=provider,
                          callback_secret=DEFAULT_CALLBACK_SECRET)
    op = _op(s, PendingAction.REFUND, "ORD-001", "REQ-R1")
    out = ad.execute_operation(s, "TENANT-A", "USER-001", "customer", op.operation_id)  # SUBMITTED
    engine = ad.make_execution_engine(s)
    res = engine.reconcile("TENANT-A")
    assert res.scanned >= 1 and res.reconciled >= 1
    assert s.get_execution_record("TENANT-A", out.execution_id).status is ExecutionStatus.CONFIRMED
    assert s.get_operation("TENANT-A", op.operation_id).status == OperationStatus.EXECUTED


def test_reconcile_unreachable_provider_goes_human():
    class BoomProvider:
        def submit(self, **kw):
            raise ProviderError("network", "down")

        def query(self, **kw):
            raise ProviderError("network", "down")

        def compensate(self, **kw):
            return {"status": "failed", "reason": "down"}

    s = MemoryStore(); _seed(s)
    # 用一个能提交成功的 provider 先创建 SUBMITTED 记录。
    ad_ok = EcommerceAdapter(execution_mode=ExecutionMode.LIVE,
                             provider=MockFundsProvider(result="success"),
                             callback_secret=DEFAULT_CALLBACK_SECRET)
    op = _op(s, PendingAction.REFUND, "ORD-001", "REQ-R2")
    out = ad_ok.execute_operation(s, "TENANT-A", "USER-001", "customer", op.operation_id)
    assert out.status is ExecutionStatus.SUBMITTED
    # 对账时外部不可达 → 记录 mismatch + 操作转人工（fail-closed，不静默）。
    engine = ExecutionEngine(s, mode=ExecutionMode.LIVE, provider=BoomProvider(),
                             callback_secret=DEFAULT_CALLBACK_SECRET)
    res = engine.reconcile("TENANT-A")
    assert s.get_execution_record("TENANT-A", out.execution_id).status is ExecutionStatus.MISMATCHED
    assert s.get_operation("TENANT-A", op.operation_id).status == OperationStatus.HUMAN_HANDOFF


# ---------------------------------------------------------------------------
# 回调 HTTP 端点（webhook）：验签 + 重放保护
# ---------------------------------------------------------------------------
def test_callback_http_endpoint_sign_and_replay():
    s = MemoryStore(); _seed(s)
    ad = EcommerceAdapter(execution_mode=ExecutionMode.LIVE,
                          provider=MockFundsProvider(result="success"),
                          callback_secret=DEFAULT_CALLBACK_SECRET)
    op = _op(s, PendingAction.REFUND, "ORD-001", "REQ-HT1")
    out = ad.execute_operation(s, "TENANT-A", "USER-001", "customer", op.operation_id)
    rec = s.get_execution_record("TENANT-A", out.execution_id)

    app = create_app(store=s, llm=MockLLM("gpt-4"), seed=False)
    with TestClient(app) as client:
        payload = {"tenant_id": "TENANT-A", "execution_id": rec.execution_id,
                   "external_txn_id": rec.external_txn_id, "status": "succeeded",
                   "amount": rec.amount, "nonce": "nonce-HT1"}
        sig = build_callback_signature(DEFAULT_CALLBACK_SECRET, payload)
        r1 = client.post("/api/callbacks/payment", json=payload, headers={"X-Signature": sig})
        assert r1.status_code == 200 and r1.json()["applied"] is True
        # 重放同 nonce → 只重放不重复生效。
        r2 = client.post("/api/callbacks/payment", json=payload, headers={"X-Signature": sig})
        assert r2.status_code == 200 and r2.json()["applied"] is False
        assert r2.json()["reason"] == "replay"
        # 坏签名 → 401。
        r3 = client.post("/api/callbacks/payment", json=payload, headers={"X-Signature": "bad"})
        assert r3.status_code == 401
    assert s.get_execution_record("TENANT-A", rec.execution_id).status is ExecutionStatus.CONFIRMED
    assert s.get_operation("TENANT-A", op.operation_id).status == OperationStatus.EXECUTED


def test_execution_read_endpoint_requires_tenant():
    s = MemoryStore(); _seed(s)
    s.create_session("TENANT-A", "USER-001", "th", time.time(), 7)
    ad = EcommerceAdapter(execution_mode=ExecutionMode.SHADOW)
    op = _op(s, PendingAction.REFUND, "ORD-001", "REQ-RO1")
    out = ad.execute_operation(s, "TENANT-A", "USER-001", "customer", op.operation_id)
    app = create_app(store=s, llm=MockLLM("gpt-4"), seed=False)
    with TestClient(app) as client:
        tok_a = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
        ok = client.get(f"/api/executions/{out.execution_id}", headers=bearer(tok_a))
        assert ok.status_code == 200 and ok.json()["status"] == "confirmed"
        # 跨租户读取 → 404（不泄露存在）。
        tok_b = issue_token("TENANT-B", "USER-B1", Role.CUSTOMER)
        denied = client.get(f"/api/executions/{out.execution_id}", headers=bearer(tok_b))
        assert denied.status_code == 404


def test_build_adapter_respects_execution_mode_setting():
    from src.config import Settings
    assert build_adapter(Settings(execution_mode="shadow")).execution_mode is ExecutionMode.SHADOW
    live = build_adapter(Settings(execution_mode="live", execution_provider="mock"))
    assert live.execution_mode is ExecutionMode.LIVE
    assert live.live_execution_enabled is True


# ---------------------------------------------------------------------------
# ❗超时未确认(overdue) → 强制对账/转人工（confirm_timeout_seconds 语义）
#   约束：状态机单向封闭、幂等锚点(operation_id/idempotency_key)不变、不加入重试/attempt 到幂等键。
# ---------------------------------------------------------------------------
def _submitted_record(s: MemoryStore, op, rid: str, *, submitted_at: float,
                      external_txn_id: str = "txn-od"):
    rec = s.create_execution_record(
        "TENANT-A", operation_id=op.operation_id, pending_action=PendingAction.REFUND,
        order_id="ORD-001", idempotency_key=op.idempotency_key, mode=ExecutionMode.LIVE,
        amount=100.0, now=time.time())
    return s.update_execution_record(
        "TENANT-A", rec.execution_id, status=ExecutionStatus.SUBMITTED,
        external_txn_id=external_txn_id, submitted_at=submitted_at)


def test_overdue_submitted_is_listed_as_reconciliation_target():
    s = MemoryStore(); _seed(s)
    engine = ExecutionEngine(s, mode=ExecutionMode.LIVE,
                             provider=MockFundsProvider(result="success"),
                             callback_secret=DEFAULT_CALLBACK_SECRET,
                             confirm_timeout_seconds=5.0)
    # 超过确认超时（提交于 10 秒前）→ 应被识别为 overdue。
    op = _op(s, PendingAction.REFUND, "ORD-001", "REQ-OD1")
    rec = _submitted_record(s, op, "REQ-OD1", submitted_at=time.time() - 10.0)
    assert [r.execution_id for r in engine.list_overdue_reconciliation_targets("TENANT-A")] == [rec.execution_id]
    # 通用对账目标也包含 overdue（overdue 优先，不因 limit 漏掉）。
    assert rec.execution_id in [r.execution_id for r in engine.list_reconciliation_targets("TENANT-A")]

    # 未超时（刚提交）记录不得进入 overdue 集合，但仍属于通用对账目标。
    op2 = _op(s, PendingAction.REFUND, "ORD-001", "REQ-OD1b")
    rec2 = _submitted_record(s, op2, "REQ-OD1b", submitted_at=time.time())
    assert rec2.execution_id not in [r.execution_id for r in engine.list_overdue_reconciliation_targets("TENANT-A")]
    assert rec2.execution_id in [r.execution_id for r in engine.list_reconciliation_targets("TENANT-A")]


def test_reconcile_overdue_unreachable_goes_human():
    class BoomProvider:
        def submit(self, **kw):
            raise ProviderError("network", "down")

        def query(self, **kw):
            raise ProviderError("network", "down")

        def compensate(self, **kw):
            return {"status": "failed", "reason": "down"}

    s = MemoryStore(); _seed(s)
    engine = ExecutionEngine(s, mode=ExecutionMode.LIVE, provider=BoomProvider(),
                             callback_secret=DEFAULT_CALLBACK_SECRET, confirm_timeout_seconds=5.0)
    op = _op(s, PendingAction.REFUND, "ORD-001", "REQ-OD2")
    rec = _submitted_record(s, op, "REQ-OD2", submitted_at=time.time() - 10.0,
                            external_txn_id="txn-od2")
    res = engine.reconcile("TENANT-A")
    # 超时未确认 + 外部不可达 → mismatch + 转人工（fail-closed，绝不静默当成功）。
    assert s.get_execution_record("TENANT-A", rec.execution_id).status is ExecutionStatus.MISMATCHED
    assert s.get_operation("TENANT-A", op.operation_id).status == OperationStatus.HUMAN_HANDOFF
    assert res.scanned >= 1 and res.mis_matched >= 1


def test_reconcile_overdue_unknown_status_goes_human():
    class UnknownProvider:
        def submit(self, **kw):
            return {"external_txn_id": "txn-u", "status": "succeeded"}

        def query(self, **kw):
            # 外部状态未知/处理中：未明确成功/失败 → 不能当作成功，须 mismatch 转人工。
            return {"external_txn_id": "txn-u", "status": "processing", "receipt": {}}

        def compensate(self, **kw):
            return {"status": "succeeded"}

    s = MemoryStore(); _seed(s)
    engine = ExecutionEngine(s, mode=ExecutionMode.LIVE, provider=UnknownProvider(),
                             callback_secret=DEFAULT_CALLBACK_SECRET, confirm_timeout_seconds=5.0)
    op = _op(s, PendingAction.REFUND, "ORD-001", "REQ-OD3")
    rec = _submitted_record(s, op, "REQ-OD3", submitted_at=time.time() - 10.0,
                            external_txn_id="txn-u")
    engine.reconcile("TENANT-A")
    assert s.get_execution_record("TENANT-A", rec.execution_id).status is ExecutionStatus.MISMATCHED
    assert s.get_operation("TENANT-A", op.operation_id).status == OperationStatus.HUMAN_HANDOFF


def test_reconcile_overdue_external_success_confirms():
    """overdue 但外部明确成功 → 对账核实后收口 confirmed（不误转人工）。"""
    s = MemoryStore(); _seed(s)
    provider = MockFundsProvider(result="success")
    engine = ExecutionEngine(s, mode=ExecutionMode.LIVE, provider=provider,
                             callback_secret=DEFAULT_CALLBACK_SECRET, confirm_timeout_seconds=5.0)
    op = _op(s, PendingAction.REFUND, "ORD-001", "REQ-OD4")
    rec = _submitted_record(s, op, "REQ-OD4", submitted_at=time.time() - 10.0,
                            external_txn_id="txn-od4")
    res = engine.reconcile("TENANT-A")
    assert s.get_execution_record("TENANT-A", rec.execution_id).status is ExecutionStatus.CONFIRMED
    assert s.get_operation("TENANT-A", op.operation_id).status == OperationStatus.EXECUTED
    assert res.scanned >= 1 and res.reconciled >= 1


# ---------------------------------------------------------------------------
# 验收加严：(4) 同一业务操作重复提交只产生一次外部执行（provider.submit 仅调用一次）
#           (6) 跨租户回调拒绝(404，不泄露存在、不污染原租户记录)
# ---------------------------------------------------------------------------
class _CountingProvider:
    """包装 MockFundsProvider，统计 submit 被外部调用的次数（验证同一操作只提交一次）。"""

    def __init__(self, result: str = "success") -> None:
        self.submits = 0
        self._inner = MockFundsProvider(result=result)

    def submit(self, **kw):
        self.submits += 1
        return self._inner.submit(**kw)

    def query(self, **kw):
        return self._inner.query(**kw)

    def compensate(self, **kw):
        return self._inner.compensate(**kw)


def test_live_idempotent_reexecute_calls_provider_once():
    """(4) 同一业务操作重复提交只产生一次外部执行：同一 operation → 同一 execution_id，
    provider.submit 仅调用 1 次，执行记录仅 1 条（绝不重复扣款/退款）。"""
    s = MemoryStore(); _seed(s)
    provider = _CountingProvider(result="success")
    ad = EcommerceAdapter(execution_mode=ExecutionMode.LIVE, provider=provider,
                          callback_secret=DEFAULT_CALLBACK_SECRET)
    op = _op(s, PendingAction.REFUND, "ORD-001", "REQ-IDEM-LIVE")
    first = ad.execute_operation(s, "TENANT-A", "USER-001", "customer", op.operation_id)
    assert first.status is ExecutionStatus.SUBMITTED
    second = ad.execute_operation(s, "TENANT-A", "USER-001", "customer", op.operation_id)
    assert second.execution_id == first.execution_id
    assert "幂等重放" in second.message
    assert provider.submits == 1  # 外部只提交一次
    assert len(s.list_execution_records("TENANT-A")) == 1


def test_callback_cross_tenant_rejected_404():
    """(6) 跨租户回调拒绝：载荷 tenant_id=TENANT-B 但 execution_id 属 TENANT-A → HTTP 404，
    且不篡改原租户执行记录/操作（未确认、未转人工、不泄露存在）。"""
    s = MemoryStore(); _seed(s)
    ad = EcommerceAdapter(execution_mode=ExecutionMode.LIVE,
                          provider=MockFundsProvider(result="success"),
                          callback_secret=DEFAULT_CALLBACK_SECRET)
    op = _op(s, PendingAction.REFUND, "ORD-001", "REQ-XT-CB")
    out = ad.execute_operation(s, "TENANT-A", "USER-001", "customer", op.operation_id)
    assert out.status is ExecutionStatus.SUBMITTED
    rec = s.get_execution_record("TENANT-A", out.execution_id)
    app = create_app(store=s, llm=MockLLM("gpt-4"), seed=False)
    with TestClient(app) as client:
        payload = {"tenant_id": "TENANT-B", "execution_id": rec.execution_id,
                   "external_txn_id": rec.external_txn_id, "status": "succeeded",
                   "amount": rec.amount, "nonce": "nonce-XT"}
        sig = build_callback_signature(DEFAULT_CALLBACK_SECRET, payload)
        r = client.post("/api/callbacks/payment", json=payload, headers={"X-Signature": sig})
        assert r.status_code == 404
    # 跨租户回调未生效：原记录仍为 SUBMITTED（未确认、未污染、未转人工）。
    assert s.get_execution_record("TENANT-A", rec.execution_id).status is ExecutionStatus.SUBMITTED
    assert s.get_operation("TENANT-A", op.operation_id).status == OperationStatus.EXECUTED


# ---------------------------------------------------------------------------
# (3) 回调超时未确认 → 强制对账/转人工，绝不静默当成功
#     本用例确定性构造：把执行记录 submitted_at 落定为「过去」 + confirm_timeout_seconds 设小，
#     不依赖真实等待；对账用「外部未终态(processing)」provider 落定为 mismatch + 转人工。
#     依赖 core-engineer(t2) 实现 _is_overdue / list_overdue_reconciliation_targets；
#     若该实现缺失，本用例会因缺方法而失败（不伪造通过）。
# ---------------------------------------------------------------------------
class _TimeoutUnknownProvider:
    """对账时外部始终处于「未终态/处理中」的 provider，用于验证超时未确认绝不静默当成功。"""

    def submit(self, **kw):
        return {"external_txn_id": "txn-timeout", "status": "succeeded",
                "receipt": {"external_txn_id": "txn-timeout", "status": "succeeded"}}

    def query(self, **kw):
        return {"external_txn_id": kw.get("external_txn_id"), "status": "processing", "receipt": {}}

    def compensate(self, **kw):
        return {"status": "failed", "reason": "processing"}


def test_execution_confirm_timeout_unconfirmed_forces_reconcile_human():
    """验收意图(3)：提交后超过 confirm_timeout_seconds 仍未 confirmed 的执行，必须被识别为
    overdue 并强制进入对账/转人工（外部未终态 → MISMATCHED + operation=HUMAN_HANDOFF），
    绝不静默当成功。确定性断言，无真实时间等待。"""
    s = MemoryStore(); _seed(s)
    ad = EcommerceAdapter(execution_mode=ExecutionMode.LIVE,
                          provider=MockFundsProvider(result="success"),
                          callback_secret=DEFAULT_CALLBACK_SECRET,
                          confirm_timeout_seconds=5.0)
    op = _op(s, PendingAction.REFUND, "ORD-001", "REQ-CTO")
    out = ad.execute_operation(s, "TENANT-A", "USER-001", "customer", op.operation_id)
    assert out.status is ExecutionStatus.SUBMITTED
    # 确定性落定 overue：submitted_at = now - (confirm_timeout + 1)，超过确认窗口。
    s.update_execution_record("TENANT-A", out.execution_id,
                              submitted_at=time.time() - (5.0 + 1.0))
    rec = s.get_execution_record("TENANT-A", out.execution_id)

    engine = ad.make_execution_engine(s)  # confirm_timeout_seconds=5.0
    # 1) 被识别为 overdue（确定性，不真实等待）。
    overdue_ids = [r.execution_id for r in engine.list_overdue_reconciliation_targets("TENANT-A")]
    assert rec.execution_id in overdue_ids
    assert engine._is_overdue(rec) is True

    # 2) 对账收口：超时未确认 + 外部未终态 → MISMATCHED + operation 转人工，绝不静默当成功。
    eng2 = ExecutionEngine(s, mode=ExecutionMode.LIVE, provider=_TimeoutUnknownProvider(),
                           callback_secret=DEFAULT_CALLBACK_SECRET, confirm_timeout_seconds=5.0)
    res = eng2.reconcile("TENANT-A")
    assert s.get_execution_record("TENANT-A", rec.execution_id).status is ExecutionStatus.MISMATCHED
    assert s.get_operation("TENANT-A", op.operation_id).status == OperationStatus.HUMAN_HANDOFF
    assert res.scanned >= 1 and res.mis_matched >= 1
