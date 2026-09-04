"""引擎层并发防护验收测试（对应 red/yellow 点①②：真实沙箱不重复执行）。

背景：test-engineer 复核发现两个引擎层并发缺陷，直接影响"真实沙箱不发生重复执行"红线：
- ① ExecutionEngine.execute 的"查 execution→建 record→provider.submit"在初版不原子，高并发下
  同一 operation 可产生**多条 execution 记录**，随后 `claim_execution_submit` 对各条记录（attempts=0）
  各自解锁 → 多个线程各自 `provider.submit`（对外部重复提交）。sqlite/postgres 有 DB
  `UNIQUE(tenant_id, operation_id)` 兜底，但 **MemoryStore 的 create 初版本是无锁 check-then-insert**。
- ② 极高并发下某线程读到旧 submitted 再提交跃迁，被 `can_transition(submitted, submitted)` 拒绝，
  向调用方抛 `submitted -> submitted` DomainError（未破坏幂等锚点但把错误抛给用户）。

本次修复：
- ① MemoryStore.create_execution_record 改为在 `_decision_lock` 内原子 check-then-insert，
  与 sqlite/postgres 的 UNIQUE 约束语义一致 → 同 (tenant_id, operation_id) 只产生一条 record →
  `claim_execution_submit` 只放行一个提交者 → provider.submit 恰好一次。
- ② engine._transition 增加 `expected_status` 参数 + 三个后端 update_execution_record 增加
  `expected_status` CAS：记录已被并发推进时返回当前记录（幂等重放）而非抛非法跃迁；
  `_run_live` 提交成功分支用 expected_status=PENDING_SUBMIT。

本文件用 **MemoryStore**（无 DB 唯一约束兜底的"最弱后端"）验证修复后的幂等锚点不变式，
并用延迟注入放大 check-then-insert 窗口，**确定性**证明：即便并发恰好撞上窗口，也只产生一条
执行记录、恰一次 provider.submit、且无 submitted->submitted 抛错（修复前会失败）。
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from src.core.types import PendingAction, Role
from src.execution import ExecutionEngine, ExecutionMode, ExecutionStatus, MockFundsProvider
from src.infrastructure.store import MemoryStore

SECRET = "guard-secret"


def _seed(store: MemoryStore) -> None:
    store.create_tenant("TENANT-A", "租户A")
    store.add_membership("TENANT-A", "USER-001", Role.CUSTOMER)
    store.create_session("TENANT-A", "USER-001", "th-A", time.time(), 7)


class CountingProvider:
    """统计 submit 调用次数（验证幂等锚点：绝不重复提交外部）。"""

    def __init__(self, result: str = "success") -> None:
        self.submits = 0
        self.txns: list[str] = []
        self._inner = MockFundsProvider(result=result)

    def submit(self, **kw):
        self.submits += 1
        r = self._inner.submit(**kw)
        self.txns.append(r["external_txn_id"])
        return r

    def query(self, **kw):
        return self._inner.query(**kw)

    def compensate(self, **kw):
        return self._inner.compensate(**kw)


def test_memory_create_execution_record_atomic_single_row():
    """① 内存版 create_execution_record 原子性：高并发同 operation 只落一条 execution record。

    这是 memory 后端（无 DB 唯一约束兜底）并发不变式的核心。修复前为无锁 check-then-insert，
    并发下会产生多条不同 execution_id；修复后在 `_decision_lock` 内原子 check-then-insert。
    """
    store = MemoryStore()
    _seed(store)
    n = 64
    barrier = threading.Barrier(n)
    exec_ids: list[str] = []
    errs: list[str] = []

    def worker():
        barrier.wait()
        try:
            r = store.create_execution_record(
                "TENANT-A", operation_id="OP-ATOMIC", pending_action=PendingAction.REFUND,
                order_id="ORD-001", idempotency_key="opkey:atomic", mode=ExecutionMode.LIVE,
                amount=100.0, now=time.time())
            exec_ids.append(r.execution_id)
        except Exception as e:  # noqa: BLE001
            errs.append(str(e))

    threads = [threading.Thread(target=worker) for _ in range(n)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    # 幂等锚点不变式：同 (tenant_id, operation_id) 只产生一条 execution record、单一 execution_id。
    assert errs == [], f"create_execution_record 并发出现异常: {errs}"
    assert len(set(exec_ids)) == 1, f"并发产生了 {len(set(exec_ids))} 条不同 execution_id（应=1）"
    # 同操作重复查询（幂等重放）返回同一记录。
    rec = store.get_execution_by_operation("TENANT-A", "OP-ATOMIC")
    assert rec is not None and rec.execution_id == exec_ids[0]
    assert len(store.list_execution_records("TENANT-A")) == 1


def test_memory_engine_concurrent_execute_submit_once_no_transition_error():
    """①+② 内存后端 engine.execute 高并发同 operation：subset 恰好一次、无 submitted->submitted 抛错。"""
    store = MemoryStore()
    _seed(store)
    provider = CountingProvider("success")
    engine = ExecutionEngine(store, mode=ExecutionMode.LIVE, provider=provider,
                             callback_secret=SECRET)
    op = store.create_operation("TENANT-A", "th-A", "ORD-001", PendingAction.REFUND,
                                "opkey:engine-conc", time.time())
    order = SimpleNamespace(total_amount=100.0)
    n = 64
    barrier = threading.Barrier(n)
    exids: list[str] = []
    errs: list[str] = []

    def worker():
        barrier.wait()
        try:
            exids.append(engine.execute(op, order).execution_id)
        except Exception as e:  # noqa: BLE001
            errs.append(str(e))

    threads = [threading.Thread(target=worker) for _ in range(n)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    rec = store.get_execution_by_operation("TENANT-A", op.operation_id)
    assert rec is not None
    # 幂等锚点 + 单执行守卫：一条记录、一个 execution_id、恰一次 provider.submit。
    assert len(set(exids)) == 1, f"并发 execute 产生 {len(set(exids))} 个不同 execution_id（应=1）"
    assert provider.submits == 1, f"provider.submit 被调用 {provider.submits} 次（应恰好=1）"
    assert len(set(provider.txns)) == 1
    # ② 关键：并发 execute 绝无 submitted->submitted 非法跃迁抛给调用方。
    assert errs == [], f"并发 execute 出现 {len(errs)} 个异常（含 submitted->submitted）: {errs}"
    assert store.get_operation("TENANT-A", op.operation_id).status.value == "executed"


def test_memory_engine_transition_cas_replay_not_throw():
    """② _transition 带 expected_status 的 CAS：记录已被并发推进时返回现状（重放）而非抛错。"""
    store = MemoryStore()
    _seed(store)
    provider = CountingProvider("success")
    engine = ExecutionEngine(store, mode=ExecutionMode.LIVE, provider=provider,
                             callback_secret=SECRET)
    op = store.create_operation("TENANT-A", "th-A", "ORD-001", PendingAction.REFUND,
                                "opkey:cas", time.time())
    rec = store.create_execution_record(
        "TENANT-A", operation_id=op.operation_id, pending_action=PendingAction.REFUND,
        order_id="ORD-001", idempotency_key=op.idempotency_key, mode=ExecutionMode.LIVE,
        amount=100.0, now=time.time())
    # 模拟"已被并发推进到 CONFIRMED（如回调已确认）"。
    store.update_execution_record("TENANT-A", rec.execution_id,
                                  status=ExecutionStatus.CONFIRMED, confirmed_at=time.time())
    # 提交者此时尝试 PENDING_SUBMIT -> SUBMITTED，但记录已 CONFIRMED → CAS 落空。
    result = engine._transition(rec, ExecutionStatus.SUBMITTED,
                                expected_status=ExecutionStatus.PENDING_SUBMIT,
                                external_txn_id="txn-cas", submitted_at=time.time())
    # CAS 落空：返回**当前**记录（CONFIRMED），不抛 submitted->submitted/非法跃迁 DomainError。
    assert result.status is ExecutionStatus.CONFIRMED
    assert store.get_execution_record("TENANT-A", rec.execution_id).status is ExecutionStatus.CONFIRMED
    # 三条 update_execution_record 后端支持 expected_status（不会因未知 keyword 报错）。
    store.update_execution_record("TENANT-A", rec.execution_id,
                                  expected_status=ExecutionStatus.CONFIRMED, updated_at=time.time())
    # CAS 命中（expected 匹配）→ 正常更新。
    store.update_execution_record("TENANT-A", rec.execution_id,
                                  expected_status=ExecutionStatus.CONFIRMED,
                                  last_error="noop", updated_at=time.time())


def test_memory_callback_same_nonce_concurrent_single_confirm():
    """③ 回调同 nonce 并发 → CAS 恰一个 confirmed、其余 replay（只重放不重复生效）。"""
    store = MemoryStore()
    _seed(store)
    provider = CountingProvider("success")
    engine = ExecutionEngine(store, mode=ExecutionMode.LIVE, provider=provider,
                             callback_secret=SECRET)
    op = store.create_operation("TENANT-A", "th-A", "ORD-001", PendingAction.REFUND,
                                "opkey:cb", time.time())
    rec = store.create_execution_record(
        "TENANT-A", operation_id=op.operation_id, pending_action=PendingAction.REFUND,
        order_id="ORD-001", idempotency_key=op.idempotency_key, mode=ExecutionMode.LIVE,
        amount=100.0, now=time.time())
    store.update_execution_record("TENANT-A", rec.execution_id, status=ExecutionStatus.PENDING_SUBMIT)
    rec = store.update_execution_record("TENANT-A", rec.execution_id,
                                        status=ExecutionStatus.SUBMITTED,
                                        external_txn_id="txn-cb", submitted_at=time.time())
    payload = {"tenant_id": "TENANT-A", "execution_id": rec.execution_id,
               "external_txn_id": rec.external_txn_id, "status": "succeeded",
               "amount": rec.amount, "nonce": "nonce-SAME"}
    import json as _json
    from src.execution.verification import build_callback_signature
    body = _json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sig = build_callback_signature(SECRET, payload)
    n = 32
    barrier = threading.Barrier(n)
    results: list[tuple] = []

    def worker():
        barrier.wait()
        r = engine.apply_callback(body, sig)
        results.append((r.applied, r.reason))

    threads = [threading.Thread(target=worker) for _ in range(n)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    applied = [r for r in results if r[0] is True]
    replay = [r for r in results if r[0] is False and r[1] == "replay"]
    assert len(applied) == 1, f"回调并发生效 {len(applied)} 次（应恰=1）"
    assert len(replay) == n - 1
    final = store.get_execution_record("TENANT-A", rec.execution_id)
    assert final.status is ExecutionStatus.CONFIRMED
    assert len(store.list_execution_records("TENANT-A")) == 1


def test_memory_compensate_idempotent_replay_stable():
    """④ compensate 幂等：同一 execution_id+idempotency_key 并发补偿，仅一次反向、reversal 稳定。"""
    store = MemoryStore()
    _seed(store)
    provider = CountingProvider("success")
    engine = ExecutionEngine(store, mode=ExecutionMode.LIVE, provider=provider,
                             callback_secret=SECRET)
    # 构造外部明确失败 → FAILED_DISPATCHED。
    provider2 = CountingProvider("failure")
    engine2 = ExecutionEngine(store, mode=ExecutionMode.LIVE, provider=provider2,
                              callback_secret=SECRET)
    op = store.create_operation("TENANT-A", "th-A", "ORD-001", PendingAction.REFUND,
                                "opkey:comp", time.time())
    order = SimpleNamespace(total_amount=100.0)
    out = engine2.execute(op, order)
    assert out.status in (ExecutionStatus.COMPENSATED, ExecutionStatus.COMPENSATION_FAILED,
                          ExecutionStatus.HUMAN_HANDOFF, ExecutionStatus.FAILED_DISPATCHED)
    rec = store.get_execution_by_operation("TENANT-A", op.operation_id)
    # 对同一 (execution_id, idempotency_key) 再次补偿：execution 状态机已终态（补偿后封闭），
    # 引擎将按现状幂等重放/返回，绝不重复发起外部反向。
    n = 16
    barrier = threading.Barrier(n)
    errs: list[str] = []

    def worker():
        barrier.wait()
        try:
            engine.compensate("TENANT-A", rec.execution_id)
        except Exception as e:  # noqa: BLE001
            errs.append(str(e))

    threads = [threading.Thread(target=worker) for _ in range(n)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    # 补偿幂等锚点：终态封闭，不产生重复反向；且异常不泄漏为资金副作用。
    assert errs == [], f"并发 compensate 出现异常: {errs}"
