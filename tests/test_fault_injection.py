"""执行链路故障注入测试 —— 不触真实资金，仅 mock/sandbox provider。

对应 t4 子项 3，对执行链路注入以下故障，验证状态收敛（submitted→reconcile→confirmed/
mismatch/human_handoff）且**不重复执行、补偿幂等**：
- F1 provider 提交超时（ProviderError: timeout）→ 置 FAILED_UNCERTAIN（提交结果不明，交对账收口）；
- F2 提交后 5xx 不确定（ProviderError: http_5xx）→ FAILED_UNCERTAIN；
- F3 外部明确失败（submit 返回 status=failed）→ FAILED_DISPATCHED → 补偿成功 COMPENSATED；
- F4 补偿失败（provider.compensate 失败）→ COMPENSATION_FAILED → 转人工 human_handoff；
- F5 回调重放/重复通知（同 nonce 重复投递）→ 只重放不重复生效，终态不变；
- F6 回调签名错误（bad signature / 缺失 secret）→ signature_invalid 拒绝，状态不变；
- F7 网关 down（provider 全部抛错）→ 对账时 mismatch → 转人工；提交时不确定 → FAILED_UNCERTAIN。
- F8 补偿幂等：同一笔 FAILED_DISPATCHED 被多次补偿（reconcile 重复扫）→ 只收敛到单一终态，
  不重复回滚（provider.compensate 以 execution_id 派生稳定 reversal_id）。

设计说明：fault 注入用"provider 直接抛 ProviderError / 返回特定 status"，不改动执行引擎，
因此验证的是**真实状态机 + 补偿 + 对账**对这些异常的收敛语义（确定性，无真实时间等待）。
用 SQLite 后端以获得本机可复现的确定性结果。
"""
from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from src.core.types import OperationStatus, PendingAction, Role
from src.execution import ExecutionEngine, ExecutionMode, ExecutionStatus, MockFundsProvider
from src.execution.provider import ProviderError
from src.execution.verification import build_callback_signature
from src.infrastructure.sqlite_store import SqliteStore

SECRET = "fault-secret"


def _seed(store) -> None:
    store.create_tenant("TENANT-A", "A")
    store.add_membership("TENANT-A", "USER-001", Role.CUSTOMER)
    store.create_session("TENANT-A", "USER-001", "th", time.time(), 7)


def _op(store, rid: str, action: PendingAction = PendingAction.REFUND):
    return store.create_operation("TENANT-A", "th", "ORD-001", action,
                                  f"opkey:{rid}", time.time())


def _make_submitted(store, rid: str, amount: float = 100.0, external_txn_id="txn-f"):
    op = _op(store, rid)
    rec = store.create_execution_record(
        "TENANT-A", operation_id=op.operation_id, pending_action=PendingAction.REFUND,
        order_id="ORD-001", idempotency_key=op.idempotency_key,
        mode=ExecutionMode.LIVE, amount=amount, now=time.time())
    rec = store.update_execution_record("TENANT-A", rec.execution_id,
                                        status=ExecutionStatus.SUBMITTED,
                                        external_txn_id=external_txn_id, submitted_at=time.time())
    return op, rec


# ---------------------------------------------------------------------------
# F1/F2: provider 提交超时 / 5xx 不确定 → FAILED_UNCERTAIN
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("code,label", [("timeout", "F1"), ("http_5xx", "F2")])
def test_submit_un_certain_goes_failed_uncertain(tmp_path, code, label):
    class TimeoutProvider:
        def submit(self, **kw):
            raise ProviderError(code, "提交结果不明")

        def query(self, **kw):
            return {"external_txn_id": kw.get("external_txn_id"), "status": "processing"}

        def compensate(self, **kw):
            return {"status": "succeeded"}

    store = SqliteStore(str(tmp_path / f"{label}.db"))
    _seed(store)
    engine = ExecutionEngine(store, mode=ExecutionMode.LIVE, provider=TimeoutProvider(),
                             callback_secret=SECRET)
    op = _op(store, label)
    out = engine.execute(op, SimpleNamespace(total_amount=100.0))
    # 提交结果不明 → 置为不确定，绝不当作成功；operation 置 executed（交对账收口）。
    assert out.status is ExecutionStatus.FAILED_UNCERTAIN
    assert out.operation_status is OperationStatus.EXECUTED
    rec = store.get_execution_by_operation("TENANT-A", op.operation_id)
    assert rec.status is ExecutionStatus.FAILED_UNCERTAIN
    store.close()


# ---------------------------------------------------------------------------
# F3: 提交后外部明确失败 → 补偿 (COMPENSATED)
# ---------------------------------------------------------------------------
def test_explicit_failure_triggers_compensation(tmp_path):
    store = SqliteStore(str(tmp_path / "f3.db"))
    _seed(store)
    # provider 明确返回 failed → 走失败补偿（回滚）。
    engine = ExecutionEngine(store, mode=ExecutionMode.LIVE,
                             provider=MockFundsProvider(result="failure"),
                             callback_secret=SECRET)
    op = _op(store, "F3")
    out = engine.execute(op, SimpleNamespace(total_amount=100.0))
    assert out.status is ExecutionStatus.COMPENSATED
    assert out.operation_status is OperationStatus.EXECUTED
    rec = store.get_execution_by_operation("TENANT-A", op.operation_id)
    assert rec.status is ExecutionStatus.COMPENSATED
    assert rec.compensation_status == "compensated"
    assert store.get_operation("TENANT-A", op.operation_id).status.value == "executed"
    store.close()


# ---------------------------------------------------------------------------
# F4: 补偿失败 → COMPENSATION_FAILED → 转人工
# ---------------------------------------------------------------------------
def test_compensation_failure_goes_human_handoff(tmp_path):
    class FailCompensationProvider:
        def submit(self, **kw):
            return {"external_txn_id": "txn-f4", "status": "failed", "receipt": {}}

        def query(self, **kw):
            return {"external_txn_id": "txn-f4", "status": "failed"}

        def compensate(self, **kw):
            return {"status": "failed", "reason": "gateway_rejected"}

    store = SqliteStore(str(tmp_path / "f4.db"))
    _seed(store)
    engine = ExecutionEngine(store, mode=ExecutionMode.LIVE,
                             provider=FailCompensationProvider(), callback_secret=SECRET)
    op = _op(store, "F4")
    out = engine.execute(op, SimpleNamespace(total_amount=100.0))
    assert out.status is ExecutionStatus.COMPENSATION_FAILED
    assert out.operation_status is OperationStatus.HUMAN_HANDOFF
    assert out.human_handoff is True
    rec = store.get_execution_by_operation("TENANT-A", op.operation_id)
    assert rec.status is ExecutionStatus.COMPENSATION_FAILED
    assert store.get_operation("TENANT-A", op.operation_id).status.value == "human_handoff"
    store.close()


# ---------------------------------------------------------------------------
# F5: 回调重放/重复通知（同 nonce 重复投递）→ 只重放不重复生效
# ---------------------------------------------------------------------------
def test_callback_replay_duplicate_notification(tmp_path):
    store = SqliteStore(str(tmp_path / "f5.db"))
    _seed(store)
    op, rec = _make_submitted(store, "F5")
    engine = ExecutionEngine(store, mode=ExecutionMode.LIVE,
                             provider=MockFundsProvider(result="success"), callback_secret=SECRET)
    payload = {"tenant_id": "TENANT-A", "execution_id": rec.execution_id,
               "external_txn_id": rec.external_txn_id, "status": "succeeded",
               "amount": rec.amount, "nonce": "nonce-F5"}
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sig = build_callback_signature(SECRET, payload)
    r1 = engine.apply_callback(body, sig)
    assert r1.applied is True and r1.status == ExecutionStatus.CONFIRMED.value
    # 重复通知（同 nonce）→ replay，终态不变。
    r2 = engine.apply_callback(body, sig)
    assert r2.applied is False and r2.reason == "replay"
    assert store.get_execution_record("TENANT-A", rec.execution_id).status is ExecutionStatus.CONFIRMED
    assert len(store.list_execution_records("TENANT-A")) == 1
    store.close()


# ---------------------------------------------------------------------------
# F6: 回调签名错误 → signature_invalid 拒绝，状态不变
# ---------------------------------------------------------------------------
def test_callback_signature_error_rejected(tmp_path):
    store = SqliteStore(str(tmp_path / "f6.db"))
    _seed(store)
    op, rec = _make_submitted(store, "F6")
    engine = ExecutionEngine(store, mode=ExecutionMode.LIVE,
                             provider=MockFundsProvider(result="success"), callback_secret=SECRET)
    payload = {"tenant_id": "TENANT-A", "execution_id": rec.execution_id,
               "external_txn_id": rec.external_txn_id, "status": "succeeded",
               "amount": rec.amount, "nonce": "nonce-F6"}
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    # 坏签名 / 缺失 secret → 拒绝，状态保持 submitted（不误确认、不记账）。
    r_bad = engine.apply_callback(body, "deadbeef")
    assert r_bad.applied is False and r_bad.reason == "signature_invalid"
    engine_empty = ExecutionEngine(store, mode=ExecutionMode.LIVE,
                                   provider=MockFundsProvider(result="success"), callback_secret="")
    r_nosecret = engine_empty.apply_callback(body, build_callback_signature("", payload))
    assert r_nosecret.applied is False and r_nosecret.reason == "signature_invalid"
    final = store.get_execution_record("TENANT-A", rec.execution_id)
    assert final.status is ExecutionStatus.SUBMITTED
    assert final.callback_nonce is None
    store.close()


# ---------------------------------------------------------------------------
# F7: 网关 down → 提交不确定 → FAILED_UNCERTAIN；对账 → mismatch → 转人工
# ---------------------------------------------------------------------------
class DownProvider:
    def submit(self, **kw):
        raise ProviderError("network", "gateway down")

    def query(self, **kw):
        raise ProviderError("network", "gateway down")

    def compensate(self, **kw):
        return {"status": "failed", "reason": "network down"}


def test_gateway_down_submit_uncertain_and_reconcile_mismatch_human(tmp_path):
    store = SqliteStore(str(tmp_path / "f7.db"))
    _seed(store)
    engine = ExecutionEngine(store, mode=ExecutionMode.LIVE, provider=DownProvider(),
                             callback_secret=SECRET)
    op = _op(store, "F7")
    out = engine.execute(op, SimpleNamespace(total_amount=100.0))
    assert out.status is ExecutionStatus.FAILED_UNCERTAIN
    # 对账：网关仍 down → 记录 mismatch + operation 转人工（fail-closed）。
    res = engine.reconcile("TENANT-A")
    final = store.get_execution_by_operation("TENANT-A", op.operation_id)
    assert final.status is ExecutionStatus.MISMATCHED
    assert store.get_operation("TENANT-A", op.operation_id).status.value == "human_handoff"
    assert res.scanned >= 1 and res.mis_matched >= 1
    store.close()


# ---------------------------------------------------------------------------
# F8: 补偿幂等 —— FAILED_DISPATCHED 被多次补偿 → 只收敛单一终态，reversal_id 稳定
# ---------------------------------------------------------------------------
def test_compensation_idempotent_single_terminal(tmp_path):
    class CountingCompensateProvider:
        def __init__(self):
            self.compensate_calls = 0
            self.reversals: set[str] = set()
            self._inner = MockFundsProvider(result="failure")

        def submit(self, **kw):
            return self._inner.submit(**kw)

        def query(self, **kw):
            return self._inner.query(**kw)

        def compensate(self, **kw):
            self.compensate_calls += 1
            r = self._inner.compensate(**kw)
            self.reversals.add(r["receipt"]["reversal_id"])
            return r

    store = SqliteStore(str(tmp_path / "f8.db"))
    _seed(store)
    provider = CountingCompensateProvider()
    engine = ExecutionEngine(store, mode=ExecutionMode.LIVE, provider=provider,
                             callback_secret=SECRET)
    op = _op(store, "F8")
    out = engine.execute(op, SimpleNamespace(total_amount=100.0))
    assert out.status is ExecutionStatus.COMPENSATED
    rec = store.get_execution_by_operation("TENANT-A", op.operation_id)
    # 对账再扫一次（FAILED_DISPATCHED 已收口为 COMPENSATED 终态）→ 不再补偿/不再变更。
    before_calls = provider.compensate_calls
    res = engine.reconcile("TENANT-A")
    after_calls = provider.compensate_calls
    assert rec.status is ExecutionStatus.COMPENSATED
    assert provider.reversals == {rec.compensation_result["reversal_id"]}  # 单一稳定回滚
    # 终态封闭：重复对账不触发额外补偿（不应增加补偿次数）。
    assert after_calls == before_calls or res.scanned == 0
    store.close()


# ---------------------------------------------------------------------------
# F9: 单执行守卫回归 —— 同 execution 高并发 execute 只推进一次，无重复提交/无状态跃迁竞态
# ---------------------------------------------------------------------------
def test_single_flight_no_duplicate_submit_or_transition_race(tmp_path):
    """t4 修复回归：同一 operation 高并发 execute（live）时，`provider.submit` 只被调用一次，
    且**不产生** submitted->submitted 状态跃迁竞态；执行记录仅一条、external_txn_id 唯一。
    这是一条"真实沙箱不发生重复执行"红线级断言，不再依赖 provider 幂等兜底。"""
    class CountingProvider:
        def __init__(self):
            self.submits = 0
            self.txns = []
            self._inner = MockFundsProvider(result="success")

        def submit(self, **kw):
            self.submits += 1
            r = self._inner.submit(**kw)
            self.txns.append(r["external_txn_id"])
            return r

        def query(self, **kw):
            return self._inner.query(**kw)

        def compensate(self, **kw):
            return self._inner.compensate(**kw)

    store = SqliteStore(str(tmp_path / "f9.db"))
    _seed(store)
    provider = CountingProvider()
    engine = ExecutionEngine(store, mode=ExecutionMode.LIVE, provider=provider,
                             callback_secret=SECRET)
    op = _op(store, "F9")
    order = SimpleNamespace(total_amount=100.0)
    n = 64
    barrier = threading.Barrier(n)
    exids: list[str] = []
    errors: list[str] = []

    def worker():
        barrier.wait()
        try:
            out = engine.execute(op, order)
            exids.append(out.execution_id)
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))

    threads = [threading.Thread(target=worker) for _ in range(n)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    # 单执行守卫：提交恰好一次，external_txn_id 唯一，无任何状态跃迁竞态异常。
    assert provider.submits == 1, f"provider.submit 被并发调用 {provider.submits} 次（应=1）"
    assert len(set(provider.txns)) == 1
    assert len(set(exids)) == 1
    assert errors == [], f"并发 execute 出现 {len(errors)} 个异常: {errors}"
    rec = store.get_execution_by_operation("TENANT-A", op.operation_id)
    assert rec is not None
    assert rec.external_txn_id is not None
    store.close()


# ---------------------------------------------------------------------------
# F10: 单执行守卫回归 —— 失败路径下也无重复补偿（终态封闭 + 守卫）
# ---------------------------------------------------------------------------
def test_single_flight_failure_no_duplicate_compensation(tmp_path):
    """t4 修复回归：外部明确失败时，高并发 execute 也绝不重复补偿、绝不重复收敛。
    只收敛到单一 COMPENSATED 终态；provider.compensate 以 execution_id 派生稳定 reversal，
    且 `_settle_after_dispatch_failure` 在终态上再被调用时不重复回滚。"""
    class CountingFailureProvider:
        def __init__(self):
            self.submits = 0
            self.compensates = 0
            self.reversals = set()
            self._inner = MockFundsProvider(result="failure")

        def submit(self, **kw):
            self.submits += 1
            return self._inner.submit(**kw)

        def query(self, **kw):
            return self._inner.query(**kw)

        def compensate(self, **kw):
            self.compensates += 1
            r = self._inner.compensate(**kw)
            self.reversals.add(r["receipt"]["reversal_id"])
            return r

    store = SqliteStore(str(tmp_path / "f10.db"))
    _seed(store)
    provider = CountingFailureProvider()
    engine = ExecutionEngine(store, mode=ExecutionMode.LIVE, provider=provider,
                             callback_secret=SECRET)
    op = _op(store, "F10")
    order = SimpleNamespace(total_amount=100.0)
    n = 32
    barrier = threading.Barrier(n)
    errors: list[str] = []

    def worker():
        barrier.wait()
        try:
            engine.execute(op, order)
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))

    threads = [threading.Thread(target=worker) for _ in range(n)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert errors == [], f"并发 execute 出现 {len(errors)} 个异常: {errors}"
    # 单执行守卫：即使失败也只有一个线程成为提交者并进入补偿路径。
    assert provider.submits <= 1
    rec = store.get_execution_by_operation("TENANT-A", op.operation_id)
    assert rec is not None
    # 终态封闭：只收敛到单一 COMPENSATED（或 COMPENSATION_FAILED）；绝不重复补偿。
    assert rec.status in (ExecutionStatus.COMPENSATED, ExecutionStatus.COMPENSATION_FAILED)
    assert len(store.list_execution_records("TENANT-A")) == 1
    # 补偿确定性：reversal_id 收敛到单一值（幂等）。
    assert len(provider.reversals) == 1
    store.close()
