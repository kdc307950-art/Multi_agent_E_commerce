"""主图节点实现（含审批与执行）。

安全红线：
- process_refund/process_return/update_return_address 一律设置 needs_approval=True，
  由唯一 human_approval 中断节点承接，任何路径都不得直接进入 execute_*。
- 模型不在能力矩阵白名单时，写操作一律转人工，不得降级为低成本模型执行。
- 执行节点必须幂等：同租户同 idempotency_key 只执行一次（重放）。
"""
from __future__ import annotations

import time

from src.core.types import (
    ApprovalStatus,
    ErrorCode,
    OperationStatus,
    PendingAction,
    generate_operation_key,
)
from src.llm.base import LLMError
from src.llm.validation import validate_intent_output, validate_order_ref
from src.graph.state import last_message_text
from src.tools import AdapterError, EcommerceAdapter


def _now() -> float:
    return time.time()


def make_nodes(llm, store, adapter: EcommerceAdapter | None = None,
               conf_threshold: float = 0.7):
    """返回绑定 llm/store 的节点函数集合。

    - adapter 缺省为 EcommerceAdapter（带租户/归属/资格校验）。
    - conf_threshold：低于该置信度的意图 fail-closed 转人工。
    """
    adapter = adapter or EcommerceAdapter()

    def _require_valid_order_id(state) -> str | None:
        """校验 order_id 格式；非法返回机器可读原因（应转人工），合法返回 None。"""
        order_id = state.get("order_id")
        if not order_id:
            return "missing_order_id"
        try:
            validate_order_ref(order_id)
        except LLMError:
            return "invalid_order_id"
        return None

    def classify_intent_node(state):
        msg = last_message_text(state["messages"])
        try:
            result = llm.classify_intent(msg)
        except LLMError:
            # 模型异常/结构化输出非法 → fail-closed 转人工（产生 error 事件）。
            return {"intent": "complaint", "confidence": 0.0, "order_id": None,
                    "low_confidence": True, "falls_to_error": True,
                    "reason": "intent_classification_failed"}
        # 严格 schema 校验：重校验一次，确保 value 在 Allow 集与 [0,1]。
        try:
            parsed = validate_intent_output(result)
        except LLMError:
            return {"intent": "complaint", "confidence": 0.0, "order_id": None,
                    "low_confidence": True, "falls_to_error": True,
                    "reason": "intent_classification_failed"}
        confidence = parsed.confidence
        if confidence < conf_threshold:
            # 低置信度 → fail-closed 转人工（route 到 escalate_ticket）。
            return {"intent": "complaint", "confidence": confidence, "order_id": parsed.order_id,
                    "low_confidence": True, "reason": "low_confidence_intent"}
        return {"intent": parsed.intent, "confidence": confidence,
                "order_id": parsed.order_id, "low_confidence": False}

    def query_order_node(state):
        tenant_id = state.get("tenant_id")
        user_id = state.get("user_id")
        role = state.get("role") or "customer"
        order_id = state.get("order_id")
        if not tenant_id or not order_id:
            return {"tool_results": ["缺少有效租户或订单号"]}
        bad = _require_valid_order_id(state)
        if bad:
            return {"tool_results": ["订单号格式非法，请核对"], "falls_to_error": True,
                    "reason": bad}
        try:
            order = adapter.get_order(tenant_id, user_id, role, order_id)
        except AdapterError:
            # 不存在或无权（同租户他人/跨租户）→ 按"不存在"处理，不泄露存在性。
            return {"tool_results": [f"订单 {order_id} 不存在"]}
        items = ", ".join(f'{i["name"]}(x{i["quantity"]})' for i in order.items)
        return {"tool_results": [f'状态:{order.status} 金额:{order.total_amount} 商品:{items}']}

    def track_shipping_node(state):
        tenant_id = state.get("tenant_id")
        user_id = state.get("user_id")
        role = state.get("role") or "customer"
        order_id = state.get("order_id")
        if not tenant_id or not order_id:
            return {"tool_results": ["缺少有效租户或订单号"]}
        bad = _require_valid_order_id(state)
        if bad:
            return {"tool_results": ["订单号格式非法，请核对"], "falls_to_error": True,
                    "reason": bad}
        try:
            order = adapter.get_order(tenant_id, user_id, role, order_id)
        except AdapterError:
            # 不可见 → 按"不存在"处理，不泄露轨迹。
            return {"tool_results": [f"订单 {order_id} 不存在"]}
        ship = adapter.get_shipping(tenant_id, user_id, role, order_id)
        if ship is None:
            return {"tool_results": [f"订单 {order_id} 暂无物流轨迹"]}
        last = ship["events"][-1]["status"] if ship["events"] else "未知"
        return {"tool_results": [f'当前状态:{last} 运单号:{ship["tracking_no"]}']}

    def _write_action(prefix: str, action: PendingAction) -> callable:
        def node(state):
            tenant_id = state.get("tenant_id")
            user_id = state.get("user_id")
            role = state.get("role") or "customer"
            order_id = state.get("order_id")
            thread_id = state.get("thread_id")
            client_request_id = state.get("client_request_id") or str(int(_now() * 1000))

            # 能力矩阵门控：写风险工具仅白名单模型可调用。
            if not llm.capability_ok:
                return {"needs_approval": False, "falls_to_error": True,
                        "reason": ErrorCode.MODEL_NOT_IN_WHITELIST.value,
                        "approval_reason": "模型档位不足，转人工"}

            if not tenant_id or not order_id:
                return {"needs_approval": False, "falls_to_error": True,
                        "reason": ErrorCode.MISSING_TENANT_CONTEXT.value,
                        "approval_reason": "缺少租户或订单上下文"}

            bad = _require_valid_order_id(state)
            if bad:
                return {"needs_approval": False, "falls_to_error": True,
                        "reason": bad, "approval_reason": "订单号非法，转人工核验"}

            # 提取并校验工具参数（归属/资格/金额/地址）+ 提取失败一律转人工。
            message = last_message_text(state["messages"])
            try:
                params = llm.extract_tool_params(action.value, message)
            except LLMError:
                return {"needs_approval": False, "falls_to_error": True,
                        "reason": "tool_param_extraction_failed",
                        "approval_reason": "无法提取工具参数，转人工"}

            try:
                if action is PendingAction.REFUND:
                    elig = adapter.check_refund_eligibility(tenant_id, user_id, role, order_id)
                    if not elig.eligible:
                        return {"needs_approval": False, "falls_to_error": True,
                                "reason": "refund_ineligible",
                                "approval_reason": elig.detail or "退款资格/金额不明，转人工核验"}
                    amount = elig.amount
                    reason = params.get("reason") or "申请退款"
                elif action is PendingAction.RETURN_REQUEST:
                    elig = adapter.check_return_eligibility(tenant_id, user_id, role, order_id)
                    if not elig.eligible:
                        return {"needs_approval": False, "falls_to_error": True,
                                "reason": "return_ineligible",
                                "approval_reason": elig.detail or "退货资格不明，转人工核验"}
                    amount = None
                    reason = params.get("reason") or "申请退货"
                else:  # return_address
                    address = adapter.validate_address_change(
                        tenant_id, user_id, role, order_id,
                        {k: v for k, v in params.items() if k != "order_id"},
                    )
                    amount = None
                    reason = f"变更退货地址至 {address['region']} {address['detail']}"
            except (AdapterError, LLMError) as exc:
                return {"needs_approval": False, "falls_to_error": True,
                        "reason": getattr(exc, "code", None) or "tool_param_invalid",
                        "approval_reason": str(exc)}

            # 校验+资格+金额通过 → 创建幂等操作与审批单。
            idem_key = generate_operation_key(action, tenant_id, order_id, client_request_id)
            op = store.create_operation(tenant_id, thread_id, order_id, action, idem_key, _now())
            approval = store.create_approval(
                tenant_id=tenant_id, thread_id=thread_id, operation_id=op.operation_id,
                action=action, order_id=order_id, amount=amount, reason=reason, now=_now(),
            )
            # 授权/执行授权信息写入 state，供 human_approval 展示与审批路由。
            return {
                "needs_approval": True,
                "pending_action": action.value,
                "operation_id": op.operation_id,
                "approval_id": approval.approval_id,
                "approval_reason": reason,
                "refund_amount": amount,
                "model": llm.model,
                "approval_status": ApprovalStatus.PENDING.value,
            }

        return node

    def escalate_ticket_node(state):
        return {"tool_results": ["已转为人工处理"]}

    def _execute(prefix: str, action: PendingAction) -> callable:
        def node(state):
            tenant_id = state.get("tenant_id")
            user_id = state.get("user_id")
            role = state.get("role") or "customer"
            operation_id = state.get("operation_id")
            thread_id = state.get("thread_id")
            order_id = state.get("order_id")
            approval_id = state.get("approval_id")

            if not tenant_id or not operation_id:
                return {"error": ErrorCode.INTERNAL_ERROR.value,
                        "final_response": "缺少执行上下文，已转人工。",
                        "needs_approval": False, "pending_action": None}

            # 复核 1：租户作用域（get_operation 已按租户校验，跨租户返回 404）。
            op = store.get_operation(tenant_id, operation_id)
            # 复核 2：动作类型必须与当前执行节点一致（防 refund 送到 execute_return）。
            if op.pending_action != action:
                return {"error": ErrorCode.APPROVAL_BINDING_MISMATCH.value,
                        "final_response": "执行节点与操作动作不匹配，已转人工。",
                        "needs_approval": False, "pending_action": None}
            # 复核 3：操作必须与当前 thread 绑定。
            if op.thread_id != thread_id:
                return {"error": ErrorCode.APPROVAL_BINDING_MISMATCH.value,
                        "final_response": "操作与当前线程不匹配，已转人工。",
                        "needs_approval": False, "pending_action": None}

            # 复核 4：审批状态必须为 approved，且绑定一致（approval -> operation/thread/action）。
            if approval_id:
                approval = store.get_approval(tenant_id, approval_id)
                if approval.status != ApprovalStatus.APPROVED:
                    return {"error": ErrorCode.APPROVAL_BINDING_MISMATCH.value,
                            "final_response": "审批未通过，拒绝执行。",
                            "needs_approval": False, "pending_action": None}
                if (approval.operation_id != op.operation_id or approval.thread_id != thread_id
                        or approval.pending_action != action):
                    return {"error": ErrorCode.APPROVAL_BINDING_MISMATCH.value,
                            "final_response": "审批绑定不一致，拒绝执行。",
                            "needs_approval": False, "pending_action": None}

            # 复核 5：模型白名单（执行节点也按评测驱动白名单复核，防低档模型写库）。
            model_name = state.get("model")
            if model_name and not llm.is_write_capable(model_name):
                return {"error": ErrorCode.MODEL_NOT_IN_WHITELIST.value,
                        "final_response": "模型档位不足，已转人工。",
                        "needs_approval": False, "pending_action": None}

            # 幂等：已执行则返回既有结果（重放），不重复执行。
            if op.status == OperationStatus.EXECUTED:
                return {"operation_id": op.operation_id,
                        "final_response": (op.result.get("message", "操作已完成。")
                                           if op.result else "操作已完成。"),
                        "error": None, "needs_approval": False, "pending_action": None}

            # 拒绝/转人工后的操作不得执行。
            if op.status in (OperationStatus.REJECTED.value, OperationStatus.HUMAN_HANDOFF.value):
                return {"error": ErrorCode.APPROVAL_BINDING_MISMATCH.value,
                        "final_response": "该操作已被拒绝或转人工，不执行。",
                        "needs_approval": False, "pending_action": None}

            # 真正执行：交给 EcommerceAdapter.execute_operation（引擎保证幂等/状态机/补偿/回执）。
            # 失败兜底：置 FAILED 并转人工，保留 operation_id 供补偿与对账。
            try:
                outcome = adapter.execute_operation(store, tenant_id, user_id, role, operation_id)
            except Exception as exc:
                store.update_operation(tenant_id, operation_id, OperationStatus.FAILED,
                                       {"error": str(exc), "message": "执行失败，已转人工对账。"})
                return {"error": ErrorCode.EXECUTION_FAILED.value,
                        "final_response": "执行失败，已转人工对账。",
                        "operation_id": operation_id, "needs_approval": False,
                        "pending_action": None, "execution_status": "failed"}

            # 引擎已把 operation 置为 outcome.operation_status（executed / human_handoff）。
            return {
                "operation_id": operation_id,
                "final_response": outcome.message,
                "error": None if not outcome.human_handoff else ErrorCode.EXECUTION_FAILED.value,
                "falls_to_error": outcome.human_handoff,
                "human_handoff": outcome.human_handoff,
                "execution_id": outcome.execution_id,
                "execution_status": outcome.status.value,
                "receipt": outcome.receipt,
                "needs_approval": False,
                "pending_action": None,
            }

        return node

    def generate_response_node(state):
        intent = state.get("intent")
        if intent == "policy" and state.get("rag_answer") is not None:
            check = state.get("hallucination_check")
            if isinstance(check, dict) and check.get("faithful") is True and not state.get("falls_to_error"):
                return {"final_response": state["rag_answer"]}
            return {"final_response": "暂时无法基于已验证的政策资料给出答复，已转人工处理。",
                    "error": state.get("reason") or "unverified_rag_answer"}
        # 非 RAG 场景：直接使用工具结果或业务执行结果。
        if state.get("operation_id") and state.get("final_response"):
            return {}
        tool_res = state.get("tool_results") or []
        if tool_res:
            text = " ".join(str(t) for t in tool_res)
            return {"final_response": llm.generate_final_response(state, text)}
        return {"final_response": llm.generate_final_response(state)}

    def handle_error_node(state):
        return {
            "final_response": "当前请求需要人工处理，已为您转人工。",
            "error": state.get("reason") or ErrorCode.INTERNAL_ERROR.value,
            "falls_to_error": True,
            # 清理残留：进入人工后不再保留待审批标记/待定动作。
            "needs_approval": False,
            "pending_action": None,
        }

    nodes = {
        "classify_intent": classify_intent_node,
        "query_order": query_order_node,
        "track_shipping": track_shipping_node,
        "process_refund": _write_action("refund", PendingAction.REFUND),
        "process_return": _write_action("return_request", PendingAction.RETURN_REQUEST),
        "update_return_address": _write_action("return_address", PendingAction.RETURN_ADDRESS),
        "escalate_ticket": escalate_ticket_node,
        "execute_refund": _execute("退款", PendingAction.REFUND),
        "execute_return": _execute("退货", PendingAction.RETURN_REQUEST),
        "execute_address_update": _execute("改址", PendingAction.RETURN_ADDRESS),
        "generate_response": generate_response_node,
        "handle_error": handle_error_node,
    }
    return nodes
