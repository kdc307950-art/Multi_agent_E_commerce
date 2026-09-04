# -*- coding: utf-8 -*-
"""Shadow 验收脚本（业务沙箱第一轮验收）。

口径（与 tests/test_execution_engine.py 对齐）：
- 用 EcommerceAdapter + MemoryStore，execution_mode=SHADOW，provider=None（不触真实资金）；
- 针对退款/退货/改址各一笔：execute_operation 后断言
    ExecutionStatus.CONFIRMED + receipt.simulated=True + operation=EXECUTED，
  且重复 execute 同一 operation 返回同一 execution_id（幂等重放不重复提交外部）；
- 归属校验：customer 仅本人订单；staff（admin）本租户；跨租户拒绝；
- 退款金额 = 订单实付（ORD-001=299.00）。

输出结构化证据到 evidence/shadow_acceptance.json。
"""
from __future__ import annotations

import json
import os
import sys
import time

# 以项目根目录为 sys.path[0]，便于直接运行本脚本时 import src。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.core.types import OperationStatus, PendingAction, Role, generate_operation_key
from src.execution import ExecutionMode, ExecutionStatus
from src.infrastructure.store import MemoryStore
from src.tools import AdapterError, EcommerceAdapter

EVAL_LABEL = "shadow_acceptance"


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
                   passed: bool, error: str | None = None) -> None:
        scenario["steps"].append({
            "step": step,
            "assertion": assertion,
            "expected": expected,
            "actual": actual,
            "result": "PASS" if passed else "FAIL",
            "error": error,
        })


REC = CheckRecorder()


def _check(scenario: dict, step: str, assertion: str, expected, actual, error=None) -> bool:
    passed = actual == expected
    REC.add_assert(scenario, step, assertion, expected, actual, passed, error)
    return passed


def _expect_error(scenario: dict, step: str, assertion: str, callable_, *args, **kw):
    """断言给定调用抛出异常（用于拒绝场景）。返回异常对象。"""
    try:
        callable_(*args, **kw)
    except Exception as exc:  # noqa: BLE001 - 拒绝场景接受任意失败
        REC.add_assert(scenario, step, assertion, "rejected", f"{type(exc).__name__}: {exc}", True)
        return exc
    REC.add_assert(scenario, step, assertion, "rejected", "no exception", False,
                   "期望拒绝但未抛异常")
    return None


def _normalize(value) -> str:
    """把枚举/对象转成可 JSON 序列化的比较文本。"""
    if hasattr(value, "value"):
        return value.value
    return value


# ---------------------------------------------------------------------------
# 数据种子
# ---------------------------------------------------------------------------
def seed(store: MemoryStore) -> None:
    store.create_tenant("TENANT-A", "tenant-a")
    store.create_tenant("TENANT-B", "tenant-b")
    store.add_membership("TENANT-A", "USER-001", Role.CUSTOMER)
    store.add_membership("TENANT-A", "USER-002", Role.CUSTOMER)
    store.add_membership("TENANT-A", "ADMIN-A", Role.ADMIN)
    store.add_membership("TENANT-B", "USER-B1", Role.CUSTOMER)


def _op(store: MemoryStore, action: PendingAction, order: str, rid: str,
        tenant: str = "TENANT-A", thread: str = "th"):
    key = generate_operation_key(action, tenant, order, rid)
    return store.create_operation(tenant, thread, order, action, key, time.time())


def _count_executions(store: MemoryStore) -> int:
    return len(store.list_execution_records("TENANT-A"))


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    store = MemoryStore()
    seed(store)
    ad = EcommerceAdapter(execution_mode=ExecutionMode.SHADOW)  # provider=None → 不触真实资金

    # --- 场景 0：退款（shadow + 幂等重放）---
    sc0 = REC.new_scenario("refund_shadow_idempotent")
    op0 = _op(store, PendingAction.REFUND, "ORD-001", "REQ-REFUND")
    order = ad.get_order("TENANT-A", "USER-001", "customer", "ORD-001")
    base0 = _count_executions(store)
    first = ad.execute_operation(store, "TENANT-A", "USER-001", "customer", op0.operation_id)
    _check(sc0, "execute", "status == CONFIRMED", ExecutionStatus.CONFIRMED, first.status)
    _check(sc0, "execute", "mode == SHADOW", ExecutionMode.SHADOW, first.mode)
    _check(sc0, "execute", "receipt.simulated is True", True,
           bool(first.receipt and first.receipt.get("simulated") is True))
    op_exec0 = store.get_operation("TENANT-A", op0.operation_id)
    _check(sc0, "execute", "operation.status == EXECUTED", OperationStatus.EXECUTED, op_exec0.status)
    rec0 = store.get_execution_record("TENANT-A", first.execution_id)
    _check(sc0, "execute", "execution record status == CONFIRMED", ExecutionStatus.CONFIRMED, rec0.status)
    _check(sc0, "execute", "execution record receipt.simulated is True", True,
           bool(rec0.receipt and rec0.receipt.get("simulated") is True))
    _check(sc0, "execute", "new execution record created", base0 + 1, _count_executions(store))

    # 幂等重放：同一 operation 再执行 → 同一 execution_id，不新增记录。
    second = ad.execute_operation(store, "TENANT-A", "USER-001", "customer", op0.operation_id)
    _check(sc0, "replay", "same execution_id", first.execution_id, second.execution_id)
    _check(sc0, "replay", "message marks idempotent replay", True, "幂等重放" in (second.message or ""))
    _check(sc0, "replay", "no new execution record", base0 + 1, _count_executions(store))
    by_op = store.get_execution_by_operation("TENANT-A", op0.operation_id)
    _check(sc0, "replay", "same execution_id via get_execution_by_operation",
           first.execution_id, by_op.execution_id if by_op else None)

    # 退款金额 = 订单实付
    _check(sc0, "refund_amount", "receipt.amount == order.total_amount",
           order.total_amount, float(first.receipt.get("amount")) if first.receipt else None)
    _check(sc0, "refund_amount", "execution record amount == order.total_amount",
           order.total_amount, rec0.amount)
    _check(sc0, "refund_amount", "order total_amount is ORD-001 实付", 299.00, order.total_amount)

    # --- 场景 1：退货（shadow + 幂等重放）---
    sc1 = REC.new_scenario("return_shadow_idempotent")
    op1 = _op(store, PendingAction.RETURN_REQUEST, "ORD-001", "REQ-RETURN")
    base1 = _count_executions(store)
    f1 = ad.execute_operation(store, "TENANT-A", "USER-001", "customer", op1.operation_id)
    _check(sc1, "execute", "status == CONFIRMED", ExecutionStatus.CONFIRMED, f1.status)
    _check(sc1, "execute", "mode == SHADOW", ExecutionMode.SHADOW, f1.mode)
    _check(sc1, "execute", "receipt.simulated is True", True,
           bool(f1.receipt and f1.receipt.get("simulated") is True))
    _check(sc1, "execute", "operation.status == EXECUTED", OperationStatus.EXECUTED,
           store.get_operation("TENANT-A", op1.operation_id).status)
    r1 = store.get_execution_record("TENANT-A", f1.execution_id)
    _check(sc1, "execute", "execution record status == CONFIRMED", ExecutionStatus.CONFIRMED, r1.status)
    _check(sc1, "execute", "new execution record created", base1 + 1, _count_executions(store))
    s1 = ad.execute_operation(store, "TENANT-A", "USER-001", "customer", op1.operation_id)
    _check(sc1, "replay", "same execution_id", f1.execution_id, s1.execution_id)
    _check(sc1, "replay", "message marks idempotent replay", True, "幂等重放" in (s1.message or ""))
    _check(sc1, "replay", "no new execution record", base1 + 1, _count_executions(store))

    # --- 场景 2：改址（shadow + 幂等重放）---
    sc2 = REC.new_scenario("address_change_shadow_idempotent")
    op2 = _op(store, PendingAction.RETURN_ADDRESS, "ORD-001", "REQ-ADDR")
    base2 = _count_executions(store)
    f2 = ad.execute_operation(store, "TENANT-A", "USER-001", "customer", op2.operation_id)
    _check(sc2, "execute", "status == CONFIRMED", ExecutionStatus.CONFIRMED, f2.status)
    _check(sc2, "execute", "mode == SHADOW", ExecutionMode.SHADOW, f2.mode)
    _check(sc2, "execute", "receipt.simulated is True", True,
           bool(f2.receipt and f2.receipt.get("simulated") is True))
    _check(sc2, "execute", "operation.status == EXECUTED", OperationStatus.EXECUTED,
           store.get_operation("TENANT-A", op2.operation_id).status)
    r2 = store.get_execution_record("TENANT-A", f2.execution_id)
    _check(sc2, "execute", "execution record status == CONFIRMED", ExecutionStatus.CONFIRMED, r2.status)
    _check(sc2, "execute", "new execution record created", base2 + 1, _count_executions(store))
    s2 = ad.execute_operation(store, "TENANT-A", "USER-001", "customer", op2.operation_id)
    _check(sc2, "replay", "same execution_id", f2.execution_id, s2.execution_id)
    _check(sc2, "replay", "message marks idempotent replay", True, "幂等重放" in (s2.message or ""))
    _check(sc2, "replay", "no new execution record", base2 + 1, _count_executions(store))

    # --- 场景 3：归属校验 ---
    sc3 = REC.new_scenario("ownership_access_control")
    # 3a：customer 仅本人订单（USER-002 执行 USER-001 的 ORD-001 退款 → 拒绝）。
    op3a = _op(store, PendingAction.REFUND, "ORD-001", "REQ-OWN-CUST", tenant="TENANT-A")
    err_a = _expect_error(sc3, "customer_own_order",
                          "USER-002(同租户非归属 customer) 执行 USER-001 订单 → 拒绝",
                          ad.execute_operation, store, "TENANT-A", "USER-002", "customer",
                          op3a.operation_id)
    _check(sc3, "customer_own_order", "order NOT executed", OperationStatus.PENDING,
           store.get_operation("TENANT-A", op3a.operation_id).status)
    # 3b：staff（admin）可本租户执行。
    op3b = _op(store, PendingAction.REFUND, "ORD-001", "REQ-OWN-STAFF", tenant="TENANT-A")
    ok_staff = ad.execute_operation(store, "TENANT-A", "ADMIN-A", "admin", op3b.operation_id)
    _check(sc3, "staff_own_tenant", "ADMIN-A(本租户 staff) 执行 → CONFIRMED",
           ExecutionStatus.CONFIRMED, ok_staff.status)
    # 3c：跨租户拒绝（TENANT-B 上下文执行 TENANT-A 操作 → 拒绝且不留执行记录）。
    op3c = _op(store, PendingAction.REFUND, "ORD-001", "REQ-OWN-XT", tenant="TENANT-A")
    before_xt = _count_executions(store)
    err_c = _expect_error(sc3, "cross_tenant",
                          "USER-B1(租户B) 执行 租户A 操作 → 拒绝",
                          ad.execute_operation, store, "TENANT-B", "USER-B1", "customer",
                          op3c.operation_id)
    _check(sc3, "cross_tenant", "跨租户不新增执行记录", before_xt, _count_executions(store))

    # ------------------------------------------------------------------
    # 汇总
    # ------------------------------------------------------------------
    all_steps = [s for sc in REC.scenarios for s in sc["steps"]]
    passed = sum(1 for s in all_steps if s["result"] == "PASS")
    failed = len(all_steps) - passed

    summary = {
        "label": EVAL_LABEL,
        "execution_mode": ExecutionMode.SHADOW.value,
        "generated_at": f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')}",
        "total_checks": len(all_steps),
        "passed": passed,
        "failed": failed,
        "passed_all": failed == 0,
        "note": ("业务沙箱 SHADOW 第一轮验收：模拟回执不触真实资金；"
                 "同一业务操作重复提交仅产生一次外部执行（幂等重放）；"
                 "退款金额以订单实付为准。"),
    }

    evidence = {
        "label": EVAL_LABEL,
        "summary": summary,
        "scenarios": REC.scenarios,
    }

    out_path = os.path.join(_ROOT, "evidence", "shadow_acceptance.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(evidence, f, ensure_ascii=False, indent=2)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"evidence_written={out_path}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
