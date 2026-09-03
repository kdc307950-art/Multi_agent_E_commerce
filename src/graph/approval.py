"""唯一人工审批中断节点。

安全红线：
- 所有敏感写操作（refund/return_request/return_address）经 process_* 设置 needs_approval=True
  后进入本节点，由唯一 human_approval interrupt 承接。
- 无 direct -> execute 绕过路径；只有审批 approved 才进入对应 execute_*。
- 审批决策写回 store 保持幂等（已决策则重放，不重复生效）。
"""
from __future__ import annotations

import time

from langgraph.types import interrupt

from src.core.types import ApprovalStatus
from src.llm.base import LLMError
from src.llm.validation import validate_resume_params


def make_approval_node(store):
    def approval_node(state: dict) -> dict:
        payload = {
            "tenant_id": state["tenant_id"],
            "approval_id": state["approval_id"],
            "operation_id": state["operation_id"],
            "pending_action": state["pending_action"],
            "order_id": state.get("order_id"),
            "amount": state.get("refund_amount"),
            "reason": state.get("approval_reason"),
            "question": "请审批此操作申请",
        }
        response = interrupt(payload)
        # 严格校验恢复参数：approved 必须为 bool；非法即 fail-closed 拒绝，绝不进入执行。
        try:
            validated = validate_resume_params(response if isinstance(response, dict) else {})
        except LLMError:
            tenant_id = state["tenant_id"]
            store.decide_approval(tenant_id, state["approval_id"], None, False,
                                  "resume_params_invalid", time.time())
            return {"approval_status": ApprovalStatus.REJECTED.value,
                    "approval_payload": {}, "needs_approval": False}
        approved = validated["approved"]
        approver = validated.get("approver")
        feedback = validated.get("feedback")
        # 写回审批决定（CAS 幂等；跨租户校验在 API 层已做；拒绝联动 operation 状态）。
        tenant_id = state["tenant_id"]
        store.decide_approval(tenant_id, state["approval_id"], approver, approved, feedback, time.time())
        return {
            "approval_status": ApprovalStatus.APPROVED.value if approved else ApprovalStatus.REJECTED.value,
            "approval_payload": response,
            # 清理残留：本节点已处理审批，needs_approval 不再有效；pending_action 保留供路由。
            "needs_approval": False,
        }

    return approval_node
