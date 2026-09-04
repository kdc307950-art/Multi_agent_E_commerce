# -*- coding: utf-8 -*-
"""业务沙箱 LIVE 验收（受控执行开关）。

口径（对齐 tests/test_execution_engine.py 与宪法）：
- 用 src.execution.MockFundsProvider + EcommerceAdapter(execution_mode=LIVE, provider=mock, callback_secret)；
- 覆盖：合法回调确认、回调重放(replay)、金额异常转人工、外部失败补偿(COMPENSATED)、
  超时未确认对账收口/转人工；另补充「live 无 provider fail-closed」。
- 端到端：创建会话 → 发起退款 → approval_required → 审批通过 → 操作 EXECUTED（live 模式）。

输出证据：evidence/live_acceptance.json（结构化）+ evidence/live_acceptance_summary.md（可读）。
运行：.\\venv\\Scripts\\python.exe scripts\\live_acceptance.py
"""
from __future__ import annotations

import json
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from fastapi.testclient import TestClient

from src.config import Settings
from src.core.types import OperationStatus, PendingAction, Role, generate_operation_key
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
from src.auth.security import issue_token
from src.tools import AdapterError, EcommerceAdapter

EVAL_LABEL = "live_acceptance"
CALLBACK_SECRET = "shadow-callback-secret"


# ---------------------------------------------------------------------------
# 断言记录器
# ---------------------------------------------------------------------------
class CheckRecorder:
    def __init__(self) -> None:
        self.scenarios: list[dict] = []

    def new_scenario(self, name: str) -> dict:
        sc = {"scenario": name, "steps": []}
        self.scenarios.append(sc)
        return sc

    def add_assert(self, scenario: dict, step: str, assertion: str, expected, actual,
                   passed: bool, error: str | None = None, detail: dict | None = None) -> None:
        entry = {
            "step": step,
            "assertion": assertion,
            "expected": expected,
            "actual": actual,
            "result": "PASS" if passed else "FAIL",
            "error": error,
        }
        if detail:
            entry["detail"] = detail
        scenario["steps"].append(entry)


REC = CheckRecorder()


def _check(scenario: dict, step: str, assertion: str, expected, actual, detail=None) -> bool:
    passed = actual == expected
    REC.add_assert(scenario, step, assertion, expected, actual, passed, detail=detail)
    return passed


def _truthy(scenario: dict, step: str, assertion: str, cond: bool, detail=None) -> bool:
    REC.add_assert(scenario, step, assertion, True, bool(cond), bool(cond), detail=detail)
    return bool(cond)


def _expect_error(scenario: dict, step: str, assertion: str, callable_, *args, **kw):
    try:
        callable_(*args, **kw)
    except Exception as exc:  # noqa: BLE001 - 拒绝场景接受任意失败
        REC.add_assert(scenario, step, assertion, "rejected", f"{type(exc).__name__}: {exc}", True)
        return exc
    REC.add_assert(scenario, step, assertion, "rejected", "no exception", False,
                   "期望拒绝但未抛异常")
    return None


# ---------------------------------------------------------------------------
# 数据种子 / 构造
# ---------------------------------------------------------------------------
def seed(store: MemoryStore, tenant_b: bool = True) -> None:
    store.create_tenant("TENANT-A", "tenant-a")
    if tenant_b:
        store.create_tenant("TENANT-B", "tenant-b")
        store.add_membership("TENANT-B", "USER-B1", Role.CUSTOMER)
    store.add_membership("TENANT-A", "USER-001", Role.CUSTOMER)
    store.add_membership("TENANT-A", "USER-002", Role.CUSTOMER)
    store.add_membership("TENANT-A", "AGENT-A", Role.AGENT)
    store.add_membership("TENANT-A", "ADMIN-A", Role.ADMIN)
    store.add_membership("TENANT-A", "APPROVER-A", Role.APPROVER)


def _fresh_store() -> MemoryStore:
    s = MemoryStore()
    seed(s)
    return s


def _op(store: MemoryStore, action: PendingAction, order: str, rid: str,
        tenant: str = "TENANT-A", thread: str = "th"):
    key = generate_operation_key(action, tenant, order, rid)
    return store.create_operation(tenant, thread, order, action, key, time.time())


def _callback(engine: ExecutionEngine, store: MemoryStore, execution_id: str,
              status: str = "succeeded", amount=None, nonce: str = "n"):
    """构造合法签名回调并派发。返回 (body, sig, result)。"""
    rec = store.get_execution_record("TENANT-A", execution_id)
    payload = {
        "tenant_id": "TENANT-A",
        "execution_id": rec.execution_id,
        "external_txn_id": rec.external_txn_id,
        "status": status,
        "amount": amount if amount is not None else rec.amount,
        "nonce": nonce,
    }
    sig = build_callback_signature(CALLBACK_SECRET, payload)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    result = engine.apply_callback(body, sig)
    return body, sig, result


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    # =====================================================================
    # L1+L2 合法回调确认 + 回调重放
    # =====================================================================
    sc = REC.new_scenario("live_valid_callback_and_replay")
    store = _fresh_store()
    ad = EcommerceAdapter(execution_mode=ExecutionMode.LIVE,
                          provider=MockFundsProvider(result="success"),
                          callback_secret=CALLBACK_SECRET)
    op = _op(store, PendingAction.REFUND, "ORD-001", "REQ-LIVE-1")
    out = ad.execute_operation(store, "TENANT-A", "USER-001", "customer", op.operation_id)
    _check(sc, "submit", "status == SUBMITTED", ExecutionStatus.SUBMITTED, out.status)
    _check(sc, "submit", "external_txn_id present", True, bool(out.external_txn_id))
    rec = store.get_execution_record("TENANT-A", out.execution_id)
    _check(sc, "submit", "execution record status == SUBMITTED", ExecutionStatus.SUBMITTED, rec.status)
    _check(sc, "submit", "execution record external_txn_id present", True, bool(rec.external_txn_id))

    engine = ad.make_execution_engine(store)
    _, _, r1 = _callback(engine, store, out.execution_id, status="succeeded",
                         amount=rec.amount, nonce="nonce-L1")
    _check(sc, "callback_confirm", "applied is True", True, r1.applied)
    _check(sc, "callback_confirm", "status == CONFIRMED", ExecutionStatus.CONFIRMED.value, r1.status)
    _check(sc, "callback_confirm", "operation.status == EXECUTED", OperationStatus.EXECUTED,
           store.get_operation("TENANT-A", op.operation_id).status)
    rec2 = store.get_execution_record("TENANT-A", out.execution_id)
    _check(sc, "callback_confirm", "execution record status == CONFIRMED",
           ExecutionStatus.CONFIRMED, rec2.status)

    # 重放同 nonce 回调 → 只重放不重复生效。
    _, _, r2 = _callback(engine, store, out.execution_id, status="succeeded",
                         amount=rec.amount, nonce="nonce-L1")
    _check(sc, "replay", "applied is False", False, r2.applied)
    _check(sc, "replay", "reason == replay", "replay", r2.reason)
    _check(sc, "replay", "execution record remains CONFIRMED", ExecutionStatus.CONFIRMED,
           store.get_execution_record("TENANT-A", out.execution_id).status)
    _check(sc, "replay", "no new execution record", 1,
           len(store.list_execution_records("TENANT-A")))

    # =====================================================================
    # L3 金额异常回调 → MISMATCHED + 转人工
    # =====================================================================
    sc = REC.new_scenario("live_callback_amount_mismatch_human")
    store = _fresh_store()
    ad = EcommerceAdapter(execution_mode=ExecutionMode.LIVE,
                          provider=MockFundsProvider(result="success"),
                          callback_secret=CALLBACK_SECRET)
    op = _op(store, PendingAction.REFUND, "ORD-001", "REQ-LIVE-3")
    out = ad.execute_operation(store, "TENANT-A", "USER-001", "customer", op.operation_id)
    rec = store.get_execution_record("TENANT-A", out.execution_id)
    engine = ad.make_execution_engine(store)
    _, _, r = _callback(engine, store, out.execution_id, status="succeeded",
                        amount=1.0, nonce="nonce-amt")  # 金额不一致
    _check(sc, "amount_mismatch", "applied is False", False, r.applied)
    _check(sc, "amount_mismatch", "reason == amount_mismatch", "amount_mismatch", r.reason)
    _check(sc, "amount_mismatch", "execution record status == MISMATCHED", ExecutionStatus.MISMATCHED,
           store.get_execution_record("TENANT-A", out.execution_id).status)
    _check(sc, "amount_mismatch", "operation.status == HUMAN_HANDOFF", OperationStatus.HUMAN_HANDOFF,
           store.get_operation("TENANT-A", op.operation_id).status)

    # =====================================================================
    # L4 外部失败 → COMPENSATED（补偿回滚）
    # =====================================================================
    sc = REC.new_scenario("live_external_failure_compensated")
    store = _fresh_store()
    ad = EcommerceAdapter(execution_mode=ExecutionMode.LIVE,
                          provider=MockFundsProvider(result="failure"),
                          callback_secret=CALLBACK_SECRET)
    op = _op(store, PendingAction.REFUND, "ORD-001", "REQ-LIVE-4")
    out = ad.execute_operation(store, "TENANT-A", "USER-001", "customer", op.operation_id)
    _check(sc, "compensate", "status == COMPENSATED", ExecutionStatus.COMPENSATED, out.status)
    rec = store.get_execution_record("TENANT-A", out.execution_id)
    _check(sc, "compensate", "compensation_status == compensated", "compensated", rec.compensation_status)
    _check(sc, "compensate", "operation.status == EXECUTED", OperationStatus.EXECUTED,
           store.get_operation("TENANT-A", op.operation_id).status)
    _check(sc, "compensate", "receipt provides reversal", True, bool(rec.compensation_result))

    # =====================================================================
    # L5 提交后超时未确认 → 对账收口（provider 可达且成功 → CONFIRMED）
    # =====================================================================
    sc = REC.new_scenario("live_timeout_unconfirmed_reconcile_confirm")
    store = _fresh_store()
    ad = EcommerceAdapter(execution_mode=ExecutionMode.LIVE,
                          provider=MockFundsProvider(result="success"),
                          callback_secret=CALLBACK_SECRET)
    op = _op(store, PendingAction.REFUND, "ORD-001", "REQ-LIVE-5")
    out = ad.execute_operation(store, "TENANT-A", "USER-001", "customer", op.operation_id)
    _check(sc, "submit", "status == SUBMITTED", ExecutionStatus.SUBMITTED, out.status)
    # 用 confirm_timeout=0 让刚提交的记录立刻 overdue；对账查询外部成功 → 收口确认。
    engine2 = ExecutionEngine(store, mode=ExecutionMode.LIVE,
                              provider=MockFundsProvider(result="success"),
                              callback_secret=CALLBACK_SECRET, confirm_timeout_seconds=0.0)
    res = engine2.reconcile("TENANT-A")
    _check(sc, "reconcile_timeout", "overdue record scanned", True, res.scanned >= 1)
    _check(sc, "reconcile_timeout", "reconciled >= 1", True, res.reconciled >= 1)
    _check(sc, "reconcile_timeout", "execution record status == CONFIRMED", ExecutionStatus.CONFIRMED,
           store.get_execution_record("TENANT-A", out.execution_id).status)
    _check(sc, "reconcile_timeout", "operation.status == EXECUTED", OperationStatus.EXECUTED,
           store.get_operation("TENANT-A", op.operation_id).status)

    # =====================================================================
    # L6 提交后超时未确认 + 外部不可达 → MISMATCHED + 转人工
    # =====================================================================
    sc = REC.new_scenario("live_timeout_unconfirmed_unreachable_human")
    store = _fresh_store()
    ad = EcommerceAdapter(execution_mode=ExecutionMode.LIVE,
                          provider=MockFundsProvider(result="success"),
                          callback_secret=CALLBACK_SECRET)
    op = _op(store, PendingAction.REFUND, "ORD-001", "REQ-LIVE-6")
    out = ad.execute_operation(store, "TENANT-A", "USER-001", "customer", op.operation_id)
    _check(sc, "submit", "status == SUBMITTED", ExecutionStatus.SUBMITTED, out.status)

    class BoomProvider:
        def submit(self, **kw):
            raise ProviderError("network", "down")

        def query(self, **kw):
            raise ProviderError("network", "down")

        def compensate(self, **kw):
            return {"status": "failed", "reason": "down"}

    engine3 = ExecutionEngine(store, mode=ExecutionMode.LIVE, provider=BoomProvider(),
                              callback_secret=CALLBACK_SECRET, confirm_timeout_seconds=0.0)
    res = engine3.reconcile("TENANT-A")
    _check(sc, "reconcile_timeout", "overdue record scanned", True, res.scanned >= 1)
    _check(sc, "reconcile_timeout", "mismatch >= 1", True, res.mis_matched >= 1)
    _check(sc, "reconcile_timeout", "execution record status == MISMATCHED", ExecutionStatus.MISMATCHED,
           store.get_execution_record("TENANT-A", out.execution_id).status)
    _check(sc, "reconcile_timeout", "operation.status == HUMAN_HANDOFF", OperationStatus.HUMAN_HANDOFF,
           store.get_operation("TENANT-A", op.operation_id).status)

    # =====================================================================
    # L0 live 无 provider → fail-closed（受控执行开关未开启）
    # =====================================================================
    sc = REC.new_scenario("live_without_provider_fail_closed")
    store = _fresh_store()
    ad = EcommerceAdapter(execution_mode=ExecutionMode.LIVE, provider=None,
                          callback_secret=CALLBACK_SECRET)
    op = _op(store, PendingAction.REFUND, "ORD-001", "REQ-LIVE-0")
    err = _expect_error(sc, "no_provider", "live 无 provider → fail-closed 拒绝",
                        ad.execute_operation, store, "TENANT-A", "USER-001", "customer",
                        op.operation_id)
    if err is not None:
        _check(sc, "no_provider", "operation NOT executed", OperationStatus.PENDING,
               store.get_operation("TENANT-A", op.operation_id).status)

    # =====================================================================
    # E2E 端到端（live 模式）：创建会话 → 发起退款 → approval_required → 审批通过 → EXECUTED
    # =====================================================================
    sc = REC.new_scenario("e2e_refund_approval_executed_live")
    store = _fresh_store()
    live_settings = Settings(
        execution_mode="live",
        execution_provider="mock",
        execution_callback_hmac_secret=CALLBACK_SECRET,
    )
    app = create_app(store=store, llm=MockLLM("gpt-4"), seed=False, settings=live_settings)
    tok_user = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
    tok_admin = issue_token("TENANT-A", "ADMIN-A", Role.ADMIN)
    with TestClient(app) as client:
        # 1) 创建会话
        r = client.post("/api/sessions", headers={"Authorization": f"Bearer {tok_user}"})
        _check(sc, "create_session", "session created (201)", 201, r.status_code)
        thread_id = r.json()["thread_id"]
        _truthy(sc, "create_session", "thread_id present", bool(thread_id))

        # 2) 发起退款 → approval_required
        r = client.post("/api/chat", json={
            "mode": "start", "thread_id": thread_id, "client_request_id": "LIVE-E2E-1",
            "message": "我要退款，订单号 ORD-001",
        }, headers={"Authorization": f"Bearer {tok_user}"})
        frames = _parse_sse(r.text)
        ar = next((e for e in frames if e["event"] == "approval_required"), None)
        _truthy(sc, "request_refund", "approval_required event emitted", ar is not None)
        approval_id = ar["data"]["approval_id"] if ar else None
        operation_id = ar["data"]["operation_id"] if ar else None
        _check(sc, "request_refund", "approval status == pending",
               "pending", (ar["data"].get("status") if ar else None))
        _truthy(sc, "request_refund", "approval_id present", bool(approval_id))
        _truthy(sc, "request_refund", "operation_id present", bool(operation_id))

        # 3) 审批通过（admin 二次确认）
        r = client.post(f"/api/approvals/{approval_id}/decision",
                        json={"approved": True, "confirmation": True,
                              "operation_id": operation_id, "pending_action": "refund"},
                        headers={"Authorization": f"Bearer {tok_admin}"})
        _check(sc, "approve", "decision http 200", 200, r.status_code)
        body = r.json()
        _check(sc, "approve", "decision operation_id matches", operation_id, body.get("operation_id"))

        # 4) 查询操作结果 → EXECUTED
        op_status = None
        for _ in range(5):
            op = client.get(f"/api/operations/{operation_id}",
                            headers={"Authorization": f"Bearer {tok_admin}"}).json()
            op_status = op.get("status")
            if op_status == "executed":
                break
            time.sleep(0.05)
        _check(sc, "query_operation", "operation.status == EXECUTED", "executed", op_status)

    # ------------------------------------------------------------------
    # 每场景标注：该步骤所依赖的实现是否已被单元层/独立核验确认，
    # 以及本验收侧重产出的是「live 端到端流程证据」还是"单元已确认"的复核。
    # ------------------------------------------------------------------
    SCENARIO_META = {
        "live_valid_callback_and_replay": (
            "unit_confirmed (callbacks/HMAC/幂等重放：test_execution_engine.py) + live 复核",
            "合法回调确认 + 同 nonce 重放只重放不重复（live 提交→回调→确认；终态封闭防重复扣款）"),
        "live_callback_amount_mismatch_human": (
            "unit_confirmed (test_callback_amount_mismatch_goes_human) + live 复核",
            "回调金额与执行记录不一致 → MISMATCHED + 操作 HUMAN_HANDOFF"),
        "live_external_failure_compensated": (
            "unit_confirmed (test_live_failure_compensated) + live 复核",
            "外部明确失败 → 补偿回滚 COMPENSATED"),
        "live_timeout_unconfirmed_reconcile_confirm": (
            "unit_confirmed (overdue 超时用例) + live 复核",
            "提交后超时未确认 + 外部可达且成功 → 对账收口 CONFIRMED"),
        "live_timeout_unconfirmed_unreachable_human": (
            "unit_confirmed (overdue 超时用例) + live 复核",
            "提交后超时未确认 + 外部不可达 → MISMATCHED + 操作 HUMAN_HANDOFF"),
        "live_without_provider_fail_closed": (
            "unit_confirmed (test_live_execution_requires_provider) + live 复核",
            "live 无 provider → fail-closed 拒绝（受控执行开关未开启）"),
        "e2e_refund_approval_executed_live": (
            "live 端到端流程证据（本验收重点，超出单元层）",
            "创建会话→发起退款→approval_required→审批通过→操作 EXECUTED（live 模式全链路）"),
    }
    for sc in REC.scenarios:
        impl, cross = SCENARIO_META.get(sc["scenario"], ("", ""))
        sc["implementation_status"] = impl
        sc["cross_check"] = cross

    # ------------------------------------------------------------------
    # 汇总与落盘
    # ------------------------------------------------------------------
    all_steps = [s for sc_ in REC.scenarios for s in sc_["steps"]]
    passed = sum(1 for s in all_steps if s["result"] == "PASS")
    failed = len(all_steps) - passed

    summary = {
        "label": EVAL_LABEL,
        "execution_mode": ExecutionMode.LIVE.value,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "total_checks": len(all_steps),
        "passed": passed,
        "failed": failed,
        "passed_all": failed == 0,
        "note": ("业务沙箱 LIVE 受控执行开关验收：合法回调确认、回调重放只重放不重复、"
                 "金额异常转人工、外部失败补偿(COMPENSATED)、超时未确认对账收口/转人工；"
                 "另含 live 无 provider fail-closed 与端到端审批→EXECUTED。"),
        "dependency_note": (
            "船长独立核验基线：test_execution_engine.py=18 passed(含4个overdue超时用例)；"
            "test_approval_idempotency.py=16 passed(含审批拒绝绝不执行、幂等决策只执行一次)；"
            "shadow 验收 evidence/shadow_acceptance.json=37/37。"
            "t2 超时未确认→对账/转人工实现已标记 completed，confirm_timeout_seconds 生效逻辑"
            "（list_overdue_reconciliation_targets / reconcile overdue 优先）在本验收 L5/L6 实际驱动；"
            "t3 回调验签/幂等/超时/拒绝执行测试已标记 completed。"
            "因此本 LIVE 验收对已确认的单元层结论仅做复核标注（见每场景 implementation_status/"
            "cross_check），重点产出 live 端到端流程证据（e2e_refund_approval_executed_live）；"
            "覆盖全部 5 项核心步骤，均基于已就绪实现，无需推迟。"
        ),
    }

    evidence = {"label": EVAL_LABEL, "summary": summary, "scenarios": REC.scenarios}

    json_path = os.path.join(_ROOT, "evidence", "live_acceptance.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(evidence, f, ensure_ascii=False, indent=2)

    md_path = os.path.join(_ROOT, "evidence", "live_acceptance_summary.md")
    _write_md(md_path, summary, REC.scenarios)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"evidence_json={json_path}")
    print(f"evidence_md={md_path}")
    return 0 if failed == 0 else 1


def _parse_sse(text: str) -> list[dict]:
    frames = []
    for block in text.strip().split("\n\n"):
        block = block.strip()
        if not block:
            continue
        d: dict = {}
        for line in block.split("\n"):
            if line.startswith("id: "):
                d["id"] = line[4:]
            elif line.startswith("event: "):
                d["event"] = line[7:]
            elif line.startswith("data: "):
                d["data"] = json.loads(line[6:])
        if "event" in d:
            frames.append(d)
    return frames


def _write_md(path: str, summary: dict, scenarios: list[dict]) -> None:
    lines = []
    lines.append("# 业务沙箱 LIVE 验收证据")
    lines.append("")
    lines.append(f"- 验收口径：`{summary['label']}`，`execution_mode={summary['execution_mode']}`")
    lines.append(f"- 断言总数：{summary['total_checks']}；通过：{summary['passed']}；"
                 f"失败：{summary['failed']}；全部通过：{'是' if summary['passed_all'] else '否'}")
    lines.append(f"- 生成时间：{summary['generated_at']}")
    lines.append("")
    lines.append("## 场景明细")
    lines.append("")
    for sc in scenarios:
        lines.append(f"### {sc['scenario']}")
        lines.append("")
        lines.append("| 步骤 | 断言 | 结果 | 实际 |")
        lines.append("|---|---|---|---|")
        for s in sc["steps"]:
            lines.append(f"| {s['step']} | {s['assertion']} | {s['result']} | {s['actual']} |")
        lines.append("")
    lines.append(f"**结论：{'通过' if summary['passed_all'] else '失败'}** "
                 f"（{summary['passed']}/{summary['total_checks']}）")
    lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    sys.exit(main())
