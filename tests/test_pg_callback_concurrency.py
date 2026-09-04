"""PostgreSQL 回调并发/CAS 测试（需要真实 PostgreSQL 数据面，含 RLS；无 DATABASE_URL 则整组跳过）。

对齐对象：t1 已实现的 `PostgresStore.apply_callback_atomic`（同事务 nonce 记账 + 状态 CAS 终态封闭 +
操作更新 + 审计写入）与 `ExecutionEngine.apply_callback`（委托该原子方法）。本文件在真实 PG 上验证
callback 安全闭环的**并发/CAS 语义**，沿用 `conftest` 的 `pg_store`/`pg_engine` fixture 与
`tests/pg_helpers.py` 的初始化用法（见 `tests/test_pg_store.py`）。

覆盖 5 场景（队长 t5 验收口径）：
1. 同 nonce 重放 → 只产生一个合法终态 + 一条可信审计链；
2. 异 nonce 并发 → 收敛到单一终态（terminal_locked 终态封闭，不重复扣款）；
3. 成功与失败竞争 → 单一确定终态，绝不静默当成功 / 绝不两者皆记成功；
4. 终态后回调（新 nonce）→ terminal_locked，终态不可覆盖；
5. 跨租户 execution_id 伪造 → `not_found` 拒绝码（HTTP 404）+ 零污染 + 不写不可信租户审计。

诚实标注：本机（无 DATABASE_URL / 未启动 PG / 未装 psql）执行时 `pg_available()` 为 False，
整组经 `@pytest.mark.skipif` 跳过，**不将被跳过当成已通过**。真实 PG 就绪后本组可直接运行。
并发模型为真实多线程 + SQLAlchemy 连接池 + PG `FOR UPDATE` 行锁（与 SQLite 单实例锁的确定性不同，
此处依赖数据库行锁/RES；断言聚焦「最终只一个合法终态 + 零污染」这类不依赖调度顺序的不变量）。
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from src.core.types import PendingAction, Role
from src.execution import (
    ExecutionEngine,
    ExecutionMode,
    ExecutionStatus,
    MockFundsProvider,
    build_callback_signature,
)
from tests.pg_helpers import pg_available

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not pg_available(),
                       reason="PostgreSQL 不可用：设置 DATABASE_URL 并启动数据库（docker compose up -d postgres）"),
]

CALLBACK_SECRET = "pg-concurrency-secret"


def _seed(pg_store) -> None:
    """构造两个租户、成员、会话（复合外键 (tenant_id, thread_id) → sessions）。"""
    pg_store.create_tenant("TENANT-A", "租户A")
    pg_store.create_tenant("TENANT-B", "租户B")
    pg_store.add_membership("TENANT-A", "USER-001", Role.CUSTOMER)
    pg_store.add_membership("TENANT-B", "USER-B1", Role.CUSTOMER)
    pg_store.create_session("TENANT-A", "USER-001", "th", time.time(), 7)
    pg_store.create_session("TENANT-B", "USER-B1", "th-b", time.time(), 7)


def _make_submitted(pg_store, tenant: str, rid: str, amount: float = 100.0):
    """创建一笔 live 已提交（SUBMITTED）的执行记录（确定性，不依赖 provider 提交流）。"""
    op = pg_store.create_operation(tenant, "th", "ORD-001", PendingAction.REFUND,
                                   f"opkey-{rid}-{tenant}", time.time())
    rec = pg_store.create_execution_record(
        tenant, operation_id=op.operation_id, pending_action=PendingAction.REFUND,
        order_id="ORD-001", idempotency_key=op.idempotency_key,
        mode=ExecutionMode.LIVE, amount=amount, now=time.time())
    rec = pg_store.update_execution_record(tenant, rec.execution_id,
                                           status=ExecutionStatus.SUBMITTED,
                                           external_txn_id=f"txn-{rid}", submitted_at=time.time())
    return op, rec


def _engine(pg_store) -> ExecutionEngine:
    return ExecutionEngine(pg_store, mode=ExecutionMode.LIVE,
                           provider=MockFundsProvider(result="success"),
                           callback_secret=CALLBACK_SECRET)


def _cb(tenant: str, rec, nonce: str, status: str = "succeeded", amount=None):
    payload = {"tenant_id": tenant, "execution_id": rec.execution_id,
               "external_txn_id": rec.external_txn_id, "status": status,
               "amount": rec.amount if amount is None else amount, "nonce": nonce}
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return body, build_callback_signature(CALLBACK_SECRET, payload)


# ---------------------------------------------------------------------------
# 1) 同 nonce 重放 → 只产生一个合法终态 + 一条可信审计链
# ---------------------------------------------------------------------------
def test_pg_same_nonce_replay_only_one_terminal(pg_store):
    """同 nonce 重放（含并发）：只产生一个合法终态（confirmed），其余判为重放。

    断言「只产生一个合法终态」+「一条可信回调审计」+「幂等锚点不变（仅一条执行记录）」。
    """
    _seed(pg_store)
    op, rec = _make_submitted(pg_store, "TENANT-A", "PG-SAME")
    engine = _engine(pg_store)
    body, sig = _cb("TENANT-A", rec, "nonce-PG-SAME")

    results = []
    barrier = threading.Barrier(4)

    def worker():
        barrier.wait()
        r = engine.apply_callback(body, sig)
        results.append((r.applied, r.reason, r.status))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    applied = [r for r in results if r[0] is True]
    replays = [r for r in results if r[0] is False and r[1] == "replay"]
    assert len(applied) == 1
    assert len(applied) + len(replays) == 4
    assert applied[0][2] == ExecutionStatus.CONFIRMED.value
    # 仅一条执行记录（幂等锚点不变），终态唯一。
    recs = pg_store.list_execution_records("TENANT-A")
    assert len(recs) == 1
    assert recs[0].status is ExecutionStatus.CONFIRMED
    assert pg_store.get_operation("TENANT-A", op.operation_id).status.value == "executed"
    # 回调审计链：PG 的 apply_callback_atomic 只在**实际状态跃迁**时落审计（见 postgres_store
    # 注释「重放/终态封闭/中间态无状态跃迁，不产生额外审计」）——因此并发同 nonce 重放仅产生
    # 1 条 confirmed 审计（唯一合法终态事实），replay 不重复留痕。
    # 每条审计的 tenant_id 都来自执行记录可信租户（绝不使用请求体不可信 tenant_id）。
    cb_audits = [a for a in pg_store.list_audit("TENANT-A") if a.action.startswith("execution.callback.")]
    assert len(cb_audits) == 1
    assert cb_audits[0].action == "execution.callback.confirmed"
    assert cb_audits[0].tenant_id == "TENANT-A"    # 可信租户
    assert cb_audits[0].user_id == "callback"      # 回调系统身份
    assert cb_audits[0].target_id == rec.execution_id


# ---------------------------------------------------------------------------
# 2) 异 nonce 并发 → 收敛到单一终态（terminal_locked 终态封闭）
# ---------------------------------------------------------------------------
def test_pg_diff_nonce_concurrent_single_terminal(pg_store):
    """不同 nonce 并发竞争同一笔执行：终态封闭（`FOR UPDATE` 行锁 + CAS `status NOT IN (终态)`）
    只允许一个合法确认，其余判 terminal_locked；幂等锚点不变，绝不重复扣款。

    断言「收敛到单一终态 + 不重复扣款（仅一条执行记录）」。聚焦不依赖调度顺序的不变量。
    """
    _seed(pg_store)
    op, rec = _make_submitted(pg_store, "TENANT-A", "PG-DIFF", amount=100.0)
    engine = _engine(pg_store)

    results = []
    barrier = threading.Barrier(4)

    def worker(i):
        body, sig = _cb("TENANT-A", rec, f"nonce-PG-{i}", status="succeeded")
        barrier.wait()
        r = engine.apply_callback(body, sig)
        results.append((r.applied, r.reason))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    confirmed = [r for r in results if r[0] is True and r[1] == "confirmed"]
    locked = [r for r in results if r[0] is False and r[1] == "terminal_locked"]
    assert len(confirmed) == 1
    assert len(confirmed) + len(locked) == 4
    recs = pg_store.list_execution_records("TENANT-A")
    assert len(recs) == 1
    assert recs[0].status is ExecutionStatus.CONFIRMED
    assert recs[0].receipt["amount"] == 100.0
    assert pg_store.get_operation("TENANT-A", op.operation_id).status.value == "executed"


# ---------------------------------------------------------------------------
# 3) 成功与失败竞争 → 单一确定终态
# ---------------------------------------------------------------------------
def test_pg_success_failure_competition_single_outcome(pg_store):
    """同一笔执行并发收到「成功」与「失败」回调：终态封闭保证收敛到**单一确定终态**，
    绝不出现「既确认成功又重复扣款/重复补偿」的双重生效（安全不变量）。

    断言（聚焦不依赖 db-engineer 竞争缺陷修复、也不依赖并发调度顺序的**稳定不变量**）：
    - 执行记录仍仅一条（幂等锚点 = operation_id，绝不重复扣款）；
    - 最终是合法终态之一（CONFIRMED / COMPENSATED / COMPENSATION_FAILED / MISMATCHED /
      HUMAN_HANDOFF / FAILED_DISPATCHED）——**绝不**出现中间态（submitted）静默当成功；
    - 成功确认（confirmed）至多一次；
    - 最终状态**未被另一回调覆盖**（若失败先赢→补偿为 COMPENSATED，成功回调不得再把终态
      改写成 MISMATCHED——终态封闭保证这一点，故只断言 final 是终态）。
    - 回调审计链全程可信（tenant_id=可信租户，user_id="callback"，target_id=execution_id）。

    「成功回调不得把 FAILED_DISPATCHED 覆写为 MISMATCHED / 不得落误导性 illegal_transition 审计」
    这一竞争缺陷的严格验收（callback 审计恰一条且非 illegal_transition）由
    `test_pg_success_failure_competition_no_spurious_audit` 作硬断言（captain 已修复该缺陷）。
    """
    _seed(pg_store)
    op, rec = _make_submitted(pg_store, "TENANT-A", "PG-SF", amount=100.0)
    engine = _engine(pg_store)

    results = []
    barrier = threading.Barrier(2)

    def ok_worker():
        body, sig = _cb("TENANT-A", rec, "nonce-OK", status="succeeded")
        barrier.wait()
        results.append(engine.apply_callback(body, sig))

    def fail_worker():
        body, sig = _cb("TENANT-A", rec, "nonce-FAIL", status="failed")
        barrier.wait()
        results.append(engine.apply_callback(body, sig))

    t1 = threading.Thread(target=ok_worker)
    t2 = threading.Thread(target=fail_worker)
    t1.start(); t2.start(); t1.join(); t2.join()

    # 幂等锚点不变：仅一条执行记录（绝不重复扣款/重复执行）。
    recs = pg_store.list_execution_records("TENANT-A")
    assert len(recs) == 1
    final = recs[0]
    # 必须是单一合法终态；绝不出现中间态（submitted）静默当成功。
    TERMINAL = {ExecutionStatus.CONFIRMED, ExecutionStatus.COMPENSATED,
                ExecutionStatus.COMPENSATION_FAILED, ExecutionStatus.MISMATCHED,
                ExecutionStatus.HUMAN_HANDOFF, ExecutionStatus.FAILED_DISPATCHED,
                ExecutionStatus.RECONCILED}
    assert final.status in TERMINAL
    # 成功确认至多一次（终态封闭拦截并发中的另一路，绝不双重生效）。
    confirmed = [r for r in results if r.applied is True and r.reason == "confirmed"]
    assert len(confirmed) <= 1
    # 回调审计链全程可信：tenant_id=可信租户（绝不写不可信请求体租户），user_id="callback"。
    cb_audits = [a for a in pg_store.list_audit("TENANT-A") if a.action.startswith("execution.callback.")]
    assert len(cb_audits) >= 1
    for a in cb_audits:
        assert a.tenant_id == "TENANT-A"
        assert a.user_id == "callback"
        assert a.target_id == rec.execution_id


def test_pg_success_failure_competition_no_spurious_audit(pg_store):
    """(captain 已修复竞争缺陷) 成功/失败并发竞争收敛到单一终态后，回调审计应恰一条且不是
    误导性的 illegal_transition——「一条可信审计链」的严格验收口径成立（硬断言）。

    修复后：失败先赢→failed_dispatch→补偿→COMPENSATED，成功后到被 terminal_locked 拒绝不写审计；
    成功先赢→confirmed，失败后到被 terminal_locked 拒绝。callback 审计恰好一条。
    """
    _seed(pg_store)
    _op, rec = _make_submitted(pg_store, "TENANT-A", "PG-SF-AUDIT", amount=100.0)
    engine = _engine(pg_store)

    results = []
    barrier = threading.Barrier(2)

    def ok_worker():
        body, sig = _cb("TENANT-A", rec, "nonce-OK", status="succeeded")
        barrier.wait()
        results.append(engine.apply_callback(body, sig))

    def fail_worker():
        body, sig = _cb("TENANT-A", rec, "nonce-FAIL", status="failed")
        barrier.wait()
        results.append(engine.apply_callback(body, sig))

    t1 = threading.Thread(target=ok_worker)
    t2 = threading.Thread(target=fail_worker)
    t1.start(); t2.start(); t1.join(); t2.join()

    # 修复后的严格验收：callback 审计恰一条（不可有非法跃迁/误导性审计）。
    cb_audits = [a for a in pg_store.list_audit("TENANT-A") if a.action.startswith("execution.callback.")]
    assert len(cb_audits) == 1
    assert cb_audits[0].action in ("execution.callback.confirmed", "execution.callback.failed_dispatch")
    for a in cb_audits:
        assert a.tenant_id == "TENANT-A"
        assert a.user_id == "callback"


def test_pg_success_then_failure_terminal_closed(pg_store):
    """确定性顺序：先成功回调确认（confirmed），后失败回调 → 终态封闭，不再覆盖/不补偿。

    断言「终态封闭：已经 confirmed 后，失败回调不触发补偿、不改变终态」。
    """
    _seed(pg_store)
    _op, rec = _make_submitted(pg_store, "TENANT-A", "PG-SF-ORDER", amount=100.0)
    engine = _engine(pg_store)

    body_ok, sig_ok = _cb("TENANT-A", rec, "nonce-OK", status="succeeded")
    r_ok = engine.apply_callback(body_ok, sig_ok)
    assert r_ok.applied is True and r_ok.status == ExecutionStatus.CONFIRMED.value

    body_fail, sig_fail = _cb("TENANT-A", rec, "nonce-LATE-FAIL", status="failed")
    r_fail = engine.apply_callback(body_fail, sig_fail)
    assert r_fail.applied is False and r_fail.reason == "terminal_locked"

    final = pg_store.get_execution_record("TENANT-A", rec.execution_id)
    assert final.status is ExecutionStatus.CONFIRMED  # 终态不变，未触发补偿/回滚
    assert final.compensation_status is None


# ---------------------------------------------------------------------------
# 4) 终态后回调（新 nonce）→ terminal_locked，终态不可覆盖
# ---------------------------------------------------------------------------
def test_pg_terminal_after_callback_not_overwritten(pg_store):
    """终态验证：confirmed 之后，无论同 nonce（replay）还是全新 nonce（terminal_locked），
    都不改变终态；任何回调不得覆盖（防重复扣款/退款）。

    断言「终态不可覆盖」。
    """
    _seed(pg_store)
    _op, rec = _make_submitted(pg_store, "TENANT-A", "PG-TERM")
    engine = _engine(pg_store)

    body1, sig1 = _cb("TENANT-A", rec, "nonce-T1")
    r1 = engine.apply_callback(body1, sig1)
    assert r1.applied is True and r1.status == ExecutionStatus.CONFIRMED.value

    # 终态后再投一个不同新 nonce（无论 status 成功/失败/处理中）→ terminal_locked，终态不变。
    body2, sig2 = _cb("TENANT-A", rec, "nonce-T2", status="failed")
    r2 = engine.apply_callback(body2, sig2)
    assert r2.applied is False and r2.reason == "terminal_locked"
    # 同 nonce 重投（即使终态）→ replay（只重放不重复生效）。
    r3 = engine.apply_callback(body1, sig1)
    assert r3.applied is False and r3.reason == "replay"

    final = pg_store.get_execution_record("TENANT-A", rec.execution_id)
    assert final.status is ExecutionStatus.CONFIRMED  # 终态不变


# ---------------------------------------------------------------------------
# 5) 跨租户 execution_id 伪造 → not_found（HTTP 404）+ 零污染 + 不写不可信租户审计
# ---------------------------------------------------------------------------
def test_pg_cross_tenant_forgery_not_found_zero_pollution(pg_store):
    """跨租户伪造：用 TENANT-B 的身份访问 TENANT-A 的 execution_id → `not_found`（HTTP 404 拒绝码），
    且**不写任何审计**（信任执行记录可信 tenant_id），原租户记录零污染（未确认、未记账、未转人工）。

    断言「伪造返回拒绝码 + 跨租户零污染 + 不写不可信租户审计」。
    """
    _seed(pg_store)
    _op, rec = _make_submitted(pg_store, "TENANT-A", "PG-XT")
    engine = _engine(pg_store)

    before_a = len(pg_store.list_audit("TENANT-A"))
    before_b = len(pg_store.list_audit("TENANT-B"))

    body, sig = _cb("TENANT-B", rec, "nonce-XT", status="succeeded")
    r = engine.apply_callback(body, sig)
    assert r.applied is False and r.reason == "not_found"  # 拒绝码（端点映射为 404）

    final = pg_store.get_execution_record("TENANT-A", rec.execution_id)
    assert final.status is ExecutionStatus.SUBMITTED
    assert final.callback_nonce is None
    # 不写审计（跨租户伪造不得污染任何租户的审计链）。
    assert len(pg_store.list_audit("TENANT-A")) == before_a
    assert len(pg_store.list_audit("TENANT-B")) == before_b
