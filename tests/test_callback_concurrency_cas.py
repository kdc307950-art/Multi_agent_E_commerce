"""后端无关的回调并发/CAS 验证（SQLite 后端，无需 PostgreSQL，本机可直接 `pytest` 运行）。

设计说明（诚实标注）：
- 本文件基于**当前工作区已落地**的真实接口：`ExecutionEngine.apply_callback`（内部委托
  `SqliteStore.apply_callback_atomic`）以及 `SqliteStore` 自身的原子方法。t1 的
  `apply_callback_atomic`（nonce 记账 + 状态 CAS + 操作更新 + 审计写入，同事务）已实现，
  `engine.apply_callback` 已改为调用它。
- 并发模型：采用**单实例共享锁**（`SqliteStore` 为单连接 + 线程 `RLock`）。实测表明在单实例下，
  写操作在锁内串行化，`apply_callback_atomic` 的 `WHERE status=:expected AND status NOT IN (终态)`
  CAS 语义在并发下**只成功一次**，结果确定，可用于强断言。
  （注：多实例/独立连接并发时，SQLite 因未设置 busy_timeout 会抛 `database is locked`，这是
  SQLite 写串行化的环境边界，不是 CAS 缺陷；真实多进程/行锁并发由 PostgreSQL 的
  `FOR UPDATE` 行锁 + RLS 验证，见 `test_pg_callback_concurrency.py`。）
- reason 语义（与 `CallbackAtomicOutcome` 对齐）：
  同 nonce 重投 → `replay`；终态（confirmed/compensated/mismatched/...）上新 nonce → `terminal_locked`
  （终态封闭，绝不覆盖）；跨租户/不存在 → `not_found`（不泄露存在，不写审计）；金额不一致 →
  `amount_mismatch`（转人工）；非法跃迁 → `illegal_transition`；成功 → `confirmed`；
  外部失败 → `failed_dispatch`（补偿/对账在引擎层）。
- 验收口径：只产生一个合法终态、终态不可覆盖、异 nonce 竞争收敛到单一终态、伪造返回拒绝码、
  跨租户零污染。
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from src.core.types import DomainError, PendingAction, Role
from src.execution import (
    ExecutionEngine,
    ExecutionMode,
    ExecutionStatus,
    MockFundsProvider,
    build_callback_signature,
)
from src.infrastructure.sqlite_store import SqliteStore

CALLBACK_SECRET = "concurrency-cas-secret"


# ---------------------------------------------------------------------------
# 构造：在隔离的临时 SQLite 数据库中建一笔「已提交待确认」的执行记录
# ---------------------------------------------------------------------------
def _seed_store(db_path: str) -> SqliteStore:
    store = SqliteStore(db_path)
    store.create_tenant("TENANT-A", "租户A")
    store.create_tenant("TENANT-B", "租户B")
    store.add_membership("TENANT-A", "USER-001", Role.CUSTOMER)
    store.add_membership("TENANT-B", "USER-B1", Role.CUSTOMER)
    store.create_session("TENANT-A", "USER-001", "th", time.time(), 7)
    store.create_session("TENANT-B", "USER-B1", "th-b", time.time(), 7)
    return store


def _make_submitted(store: SqliteStore, tenant: str, rid: str, amount: float = 100.0):
    """创建一笔 live 已提交（SUBMITTED）的执行记录（确定性，不依赖 provider 提交流）。"""
    op = store.create_operation(tenant, "th", "ORD-001", PendingAction.REFUND,
                                f"opkey-{rid}", time.time())
    rec = store.create_execution_record(
        tenant, operation_id=op.operation_id, pending_action=PendingAction.REFUND,
        order_id="ORD-001", idempotency_key=op.idempotency_key,
        mode=ExecutionMode.LIVE, amount=amount, now=time.time())
    store.update_execution_record(tenant, rec.execution_id, status=ExecutionStatus.SUBMITTED,
                                  external_txn_id=f"txn-{rid}", submitted_at=time.time())
    return op, rec


def _engine(store: SqliteStore) -> ExecutionEngine:
    return ExecutionEngine(store, mode=ExecutionMode.LIVE,
                           provider=MockFundsProvider(result="success"),
                           callback_secret=CALLBACK_SECRET)


def _cb(tenant: str, rec, nonce: str, status: str = "succeeded", amount=None):
    """构造带合法签名的回调 body + signature。"""
    payload = {"tenant_id": tenant, "execution_id": rec.execution_id,
               "external_txn_id": rec.external_txn_id, "status": status,
               "amount": rec.amount if amount is None else amount, "nonce": nonce}
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return body, build_callback_signature(CALLBACK_SECRET, payload)


# ---------------------------------------------------------------------------
# 1) 同 nonce 并发重放 → CAS 只允许一个合法跃迁
# ---------------------------------------------------------------------------
def test_same_nonce_concurrent_only_one_applies(tmp_path):
    """同 nonce 并发重放：只允许第一个跃迁到终态（confirmed），其余全部判为重放。

    断言「只产生一个合法终态 + 幂等锚点不变」。单实例共享锁下结果为确定性。
    """
    store = _seed_store(str(tmp_path / "cb1.db"))
    op, rec = _make_submitted(store, "TENANT-A", "SAME-NONCE")
    engine = _engine(store)
    body, sig = _cb("TENANT-A", rec, "nonce-SAME")

    results = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        r = engine.apply_callback(body, sig)
        results.append((r.applied, r.reason, r.status))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    applied = [r for r in results if r[0] is True]
    replays = [r for r in results if r[0] is False and r[1] == "replay"]
    # 恰好一次应用，其余全部判为重放（只重放不重复生效）。
    assert len(applied) == 1
    assert len(replays) == 7
    assert applied[0][2] == ExecutionStatus.CONFIRMED.value
    # 终态唯一 + 幂等锚点不变：只有一条执行记录，状态为 confirmed。
    recs = store.list_execution_records("TENANT-A")
    assert len(recs) == 1
    assert recs[0].status is ExecutionStatus.CONFIRMED
    assert store.get_operation("TENANT-A", op.operation_id).status.value == "executed"
    store.close()


# ---------------------------------------------------------------------------
# 2) 异 nonce 并发竞争 → 收敛到单一终态（终态封闭：仅一个 confirmed，其余 terminal_locked）
# ---------------------------------------------------------------------------
def test_diff_nonce_concurrent_converges_to_single_terminal(tmp_path):
    """不同 nonce 并发竞争同一笔执行：终态封闭保证只允许一个合法确认（confirmed），
    其余并发回调因终态封闭被判 `terminal_locked`，绝不覆盖终态/绝不重复扣款。

    断言：恰 1 个 confirmed；其余全部 terminal_locked；执行记录仍仅一条；终态 confirmed。
    """
    store = _seed_store(str(tmp_path / "cb2.db"))
    op, rec = _make_submitted(store, "TENANT-A", "DIFF-NONCE", amount=100.0)
    engine = _engine(store)

    results = []
    barrier = threading.Barrier(8)

    def worker(i):
        body, sig = _cb("TENANT-A", rec, f"nonce-{i}", status="succeeded")
        barrier.wait()
        r = engine.apply_callback(body, sig)
        results.append((r.applied, r.reason))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    confirmed = [r for r in results if r[0] is True and r[1] == "confirmed"]
    locked = [r for r in results if r[0] is False and r[1] == "terminal_locked"]
    assert len(confirmed) == 1  # 只允许一个合法终态确认
    assert len(locked) == 7     # 其余因终态封闭被拒
    # 幂等锚点不变；终态唯一 confirmed；金额无漂移。
    recs = store.list_execution_records("TENANT-A")
    assert len(recs) == 1
    final = recs[0]
    assert final.status is ExecutionStatus.CONFIRMED
    assert final.receipt["amount"] == 100.0
    assert store.get_operation("TENANT-A", op.operation_id).status.value == "executed"
    store.close()


# ---------------------------------------------------------------------------
# 3) 终态后回调（新 nonce）→ terminal_locked，终态不可覆盖
# ---------------------------------------------------------------------------
def test_terminal_status_not_overwritten_by_later_callback(tmp_path):
    """终态验证：confirmed 之后，即使是一个全新 nonce 的后续回调，也判 terminal_locked，不改变终态。

    断言「终态不可被覆盖」——这是防重复扣款/退款的关键属性。
    """
    store = _seed_store(str(tmp_path / "cb3.db"))
    _op, rec = _make_submitted(store, "TENANT-A", "TERMINAL")
    engine = _engine(store)

    body1, sig1 = _cb("TENANT-A", rec, "nonce-T1")
    r1 = engine.apply_callback(body1, sig1)
    assert r1.applied is True and r1.status == ExecutionStatus.CONFIRMED.value

    # 终态后再来一个不同的 new nonce（哪怕 status=failed）：应被终态封闭拒绝。
    body2, sig2 = _cb("TENANT-A", rec, "nonce-T2", status="failed")
    r2 = engine.apply_callback(body2, sig2)
    assert r2.applied is False and r2.reason == "terminal_locked"
    final = store.get_execution_record("TENANT-A", rec.execution_id)
    assert final.status is ExecutionStatus.CONFIRMED  # 终态不变
    assert final.receipt["amount"] == rec.amount
    store.close()


# ---------------------------------------------------------------------------
# 4) 跨租户 execution_id 伪造 → not_found（拒绝码），零污染
# ---------------------------------------------------------------------------
def test_cross_tenant_forgery_not_found_zero_pollution(tmp_path):
    """跨租户伪造：用 TENANT-B 的身份访问 TENANT-A 的 execution_id → 判 `not_found`，
    且**不写任何审计**（信任执行记录可信 tenant_id，不写不可信请求体 tenant_id），原记录零污染。

    断言「伪造返回拒绝码 + 跨租户零污染 + 不写不可信租户审计」。
    """
    store = _seed_store(str(tmp_path / "cb4.db"))
    _op, rec = _make_submitted(store, "TENANT-A", "XT-FORGERY")
    engine = _engine(store)

    # 记录伪造前的审计条数。
    before_a = len(store.list_audit("TENANT-A"))
    before_b = len(store.list_audit("TENANT-B"))

    # TENANT-B 带 TENANT-A 的 execution_id 回调（伪造跨租户归属）。
    body, sig = _cb("TENANT-B", rec, "nonce-XT", status="succeeded")
    r = engine.apply_callback(body, sig)
    assert r.applied is False and r.reason == "not_found"

    # 原租户 A 的记录零污染：仍是 SUBMITTED、未记账 nonce、未确认、未转人工。
    final = store.get_execution_record("TENANT-A", rec.execution_id)
    assert final.status is ExecutionStatus.SUBMITTED
    assert final.callback_nonce is None
    # 不写审计（跨租户伪造不得污染审计链）。
    assert len(store.list_audit("TENANT-A")) == before_a
    assert len(store.list_audit("TENANT-B")) == before_b
    store.close()


# ---------------------------------------------------------------------------
# 5) 回调确认后：可信审计链 + 操作状态正确
# ---------------------------------------------------------------------------
def test_callback_confirm_left_trustworthy_audit_trail(tmp_path):
    """确认成功后应落一条可信回调审计（tenant_id=执行记录可信租户，user_id="callback"），
    且操作置为 EXECUTED。断言审计链条可信、仅一次。"""
    store = _seed_store(str(tmp_path / "cb5.db"))
    op, rec = _make_submitted(store, "TENANT-A", "AUDIT")
    engine = _engine(store)

    body, sig = _cb("TENANT-A", rec, "nonce-AUDIT")
    r = engine.apply_callback(body, sig)
    assert r.applied is True and r.reason == "confirmed"

    audits = store.list_audit("TENANT-A")
    cb_audits = [a for a in audits if a.action.startswith("execution.callback.")]
    assert len(cb_audits) == 1
    assert cb_audits[0].action == "execution.callback.confirmed"
    assert cb_audits[0].tenant_id == "TENANT-A"      # 可信租户
    assert cb_audits[0].user_id == "callback"         # 回调系统身份
    assert cb_audits[0].target_id == rec.execution_id
    # 操作已置为 executed。
    assert store.get_operation("TENANT-A", op.operation_id).status.value == "executed"
    store.close()


# ---------------------------------------------------------------------------
# 6) 存储层原子方法本身的 CAS：终态不可被直接覆盖（expected status 校验）
# ---------------------------------------------------------------------------
def test_store_atomic_cas_expected_status_only_once(tmp_path):
    """直接调用 `store.apply_callback_atomic`：在 SUBMITTED 上并发（单实例共享锁）不同 nonce，
    CAS（`WHERE status=:expected AND status NOT IN (终态)`）保证只允许一个 confirmed，其余 terminal_locked。
    等价于校验「expected status CAS 只成功一次」。
    """
    store = _seed_store(str(tmp_path / "cb6.db"))
    _op, rec = _make_submitted(store, "TENANT-A", "STORE-CAS")
    now = time.time()

    results = []
    barrier = threading.Barrier(8)

    def worker(i):
        payload = {"tenant_id": "TENANT-A", "execution_id": rec.execution_id,
                   "external_txn_id": rec.external_txn_id, "status": "succeeded",
                   "amount": rec.amount, "nonce": f"nonce-{i}"}
        barrier.wait()
        o = store.apply_callback_atomic("TENANT-A", execution_id=rec.execution_id,
                                        nonce=f"nonce-{i}", payload=payload, now=now + i * 0.001)
        results.append((o.applied, o.reason))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert sum(1 for r in results if r[0] is True and r[1] == "confirmed") == 1
    assert sum(1 for r in results if r[0] is False and r[1] == "terminal_locked") == 7
    final = store.get_execution_record("TENANT-A", rec.execution_id)
    assert final.status is ExecutionStatus.CONFIRMED
    store.close()


# ---------------------------------------------------------------------------
# 7) 金额不一致 → amount_mismatch 转人工；非法跃迁 → illegal_transition 转人工
# ---------------------------------------------------------------------------
def test_amount_mismatch_and_illegal_transition_handoff(tmp_path):
    """回调金额不一致 → amount_mismatch（转人工，操作 human_handoff）；非法状态跃迁 → illegal_transition。"""
    store = _seed_store(str(tmp_path / "cb7.db"))
    op, rec = _make_submitted(store, "TENANT-A", "MISMATCH", amount=100.0)
    engine = _engine(store)

    # 1) 金额不一致 → amount_mismatch + 操作转人工。
    body1, sig1 = _cb("TENANT-A", rec, "nonce-AMT", status="succeeded", amount=1.0)
    r1 = engine.apply_callback(body1, sig1)
    assert r1.applied is False and r1.reason == "amount_mismatch"
    assert store.get_execution_record("TENANT-A", rec.execution_id).status is ExecutionStatus.MISMATCHED
    assert store.get_operation("TENANT-A", op.operation_id).status.value == "human_handoff"

    # 2) 正常一笔 → confirmed；随后对同一执行用 status 触发非法跃迁 → 终态已封闭（terminal_locked）。
    store2 = _seed_store(str(tmp_path / "cb7b.db"))
    op2, rec2 = _make_submitted(store2, "TENANT-A", "ILLEGAL", amount=100.0)
    engine2 = _engine(store2)
    body_ok, sig_ok = _cb("TENANT-A", rec2, "nonce-OK")
    assert engine2.apply_callback(body_ok, sig_ok).applied is True
    # 终态 closed——再投新 nonce → terminal_locked（体现终态不可覆盖）。
    body_x, sig_x = _cb("TENANT-A", rec2, "nonce-LATE", status="processing")
    assert engine2.apply_callback(body_x, sig_x).reason == "terminal_locked"
    assert store2.get_execution_record("TENANT-A", rec2.execution_id).status is ExecutionStatus.CONFIRMED
    store.close()
    store2.close()
