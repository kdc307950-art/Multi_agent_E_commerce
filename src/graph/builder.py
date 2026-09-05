"""LangGraph 主图构建。

无 direct -> execute 绕过：process_refund / process_return / update_return_address
一律 add_edge 到唯一 human_approval，审批结果按 pending_action 单一路由到对应 execute_*。

CrewAI 主链路：crewai_enabled=True 且意图绑定到 crewai 工具（order/refund/complaint）时，
route_by_intent 进入 crewai_refund_agent（构造 build_crewai_router 并调用 run_business_task）；
否则完全走现有确定性节点（不改变现状）。写意图（refund）经 crewai 后仍必须唯一进入
human_approval，绝不引入 direct -> execute 绕过；任何 crewai 异常/端点不在白名单/模型不在
HIGH_CONFIDENCE_MODELS/无自托管 LLM 一律 fail-closed 走 handle_error（转人工）。
"""
from __future__ import annotations

import time

from langgraph.graph import END, START, StateGraph

from src.core.types import ApprovalStatus
from src.graph.approval import make_approval_node
from src.graph.nodes import make_nodes
from src.graph.rag import make_rag_graph
from src.graph.state import AgentState
from src.tools import EcommerceAdapter, build_crewai_router

# 绑定到 CrewAI 真实工具子集的意图（order/refund/complaint）。这些意图在 crewai_enabled 时
# 进入 crewai_refund_agent；其余意图完全走既有确定性节点（不改变现状）。
CREWAI_BOUND_INTENTS: frozenset[str] = frozenset({"order", "refund", "complaint"})

# 敏感写意图 -> 审批动作（PendingAction）。写操作经 crewai 后仍必须唯一进入 human_approval，
# 绝不引入 direct -> execute 绕过边。
CREWAI_WRITE_INTENTS: dict[str, str] = {
    "refund": "refund",
    "return_request": "return_request",
    "return_address": "return_address",
}


def route_condition(state: AgentState) -> str:
    intent = state.get("intent")
    m = {
        "order": "query_order",
        "shipping": "track_shipping",
        "refund": "process_refund",
        "return_request": "process_return",
        "return_address": "update_return_address",
        "policy": "agentic_rag",
        "complaint": "escalate_ticket",
        "other": "generate_response",
    }
    return m.get(intent, "generate_response")


def approval_result_condition(state: AgentState) -> str:
    """审批结果路由：approved 按 pending_action 进入对应执行节点；否则 handle_error。"""
    if state.get("approval_status") != "approved":
        return "rejected"
    m = {
        "refund": "approved_refund",
        "return_request": "approved_return",
        "return_address": "approved_address",
    }
    return m.get(state.get("pending_action"), "rejected")


def rag_result_condition(state: AgentState) -> str:
    return "error" if state.get("falls_to_error") else "done"


def write_approval_condition(state: AgentState) -> str:
    """写节点后置条件：只有成功创建待审批操作（needs_approval）才进入唯一 human_approval；
    资格/参数/能力校验失败一律 fail-closed 转 handle_error（转人工），绝不绕过审批或直接执行。"""
    if state.get("needs_approval") and state.get("approval_id"):
        return "approve"
    return "handle_error"


def build_graph(llm, store, checkpointer, retriever=None, adapter=None, conf_threshold=0.7,
                settings=None):
    """构建并编译主图（checkpointer 注入以支持中断恢复与审批续跑）。

    - settings：可选；提供 crewai_enabled / 自托管 LLM 配置 / 网络白名单。缺省（None）时
      crewai_enabled=False，主路径与既有确定性行为完全一致。
    - crewai_enabled=True 且意图绑定到 crewai 工具（order/refund/complaint）时，
      route_by_intent 进入 crewai_refund_agent（构造 build_crewai_router 并调用
      run_business_task）；否则完全走现有确定性节点（不改变现状）。
    - 写意图（refund）经 crewai 后仍必须经唯一 human_approval；绝不 direct -> execute。
    """
    crewai_enabled = bool(settings is not None and getattr(settings, "crewai_enabled", False))
    effective_adapter = adapter or EcommerceAdapter()
    nodes = make_nodes(llm, store, adapter=effective_adapter, conf_threshold=conf_threshold)
    rag_graph = make_rag_graph(llm, retriever=retriever)
    approval_node = make_approval_node(store)

    def crewai_route_condition(state: AgentState) -> str:
        """意图路由：crewai 启用且意图绑定到 crewai 工具 → crewai_refund_agent；否则走既有映射。"""
        intent = state.get("intent")
        if crewai_enabled and intent in CREWAI_BOUND_INTENTS:
            return "crewai_refund_agent"
        return route_condition(state)

    def crewai_result_condition(state: AgentState) -> str:
        """crewai 节点后置：写意图且成功创建待审批 → 唯一 human_approval；
        失败/异常 → handle_error（fail-closed 转人工）；只读/升级 → generate_response。"""
        if state.get("needs_approval") and state.get("approval_id"):
            return "approve"
        if state.get("falls_to_error"):
            return "handle_error"
        return "done"

    def _fail_closed(state: AgentState, code: str, message: str) -> dict:
        """fail-closed 负载：转人工，保留必要状态，绝不继续执行/绕过审批。"""
        return {
            "falls_to_error": True,
            "reason": code,
            "needs_approval": False,
            "pending_action": None,
            "approval_reason": f"{message}；已转人工核验",
            "tool_results": [f"{message}；已转人工核验"],
        }

    def crewai_refund_agent_node(state: AgentState) -> dict:
        intent = state.get("intent")
        if not crewai_enabled or intent not in CREWAI_BOUND_INTENTS:
            # 防御：不应被路由到；由 crewai_route_condition 保证不会发生（保持现状）。
            return {"needs_approval": False, "reason": "crewai_not_applicable"}
        try:
            router = build_crewai_router(settings, adapter=effective_adapter, store=store)
        except Exception as exc:  # crewai 依赖缺失/配置非法 → fail-closed 转人工
            code = getattr(exc, "code", None) or "crewai_router_build_failed"
            return _fail_closed(state, code, f"CrewAI 路由构造失败：{exc}")
        # 服务端 TenantContext 经闭包注入工具；模型/客户端不可覆盖 tenant/user/role。
        ctx = {
            "tenant_id": state.get("tenant_id"),
            "user_id": state.get("user_id"),
            "role": state.get("role") or "customer",
            "order_id": state.get("order_id"),
            "thread_id": state.get("thread_id"),
            "client_request_id": state.get("client_request_id") or str(int(time.time() * 1000)),
        }
        params = {"order_id": state.get("order_id")}
        try:
            result = router.run_business_task(intent, ctx, params)
        except Exception as exc:
            code = getattr(exc, "code", None) or "crewai_delegation_failed"
            return _fail_closed(state, code, f"CrewAI 委派失败：{exc}")
        result = dict(result or {})
        tool = result.get("tool") or router.resolve_tool(intent)
        crew_result = result.get("crew_result") or result.get("message") or ""
        out = {"tool": tool, "intent": intent, "crew_result": crew_result,
               "reason": None, "falls_to_error": False}
        action = CREWAI_WRITE_INTENTS.get(intent)
        if action:
            approval_id = result.get("approval_id")
            operation_id = result.get("operation_id")
            if approval_id and operation_id:
                # 写意图 → 唯一 human_approval（绝不执行）。
                out.update({
                    "needs_approval": True,
                    "approval_id": approval_id,
                    "operation_id": operation_id,
                    "pending_action": action,
                    "approval_status": ApprovalStatus.PENDING.value,
                    "approval_reason": result.get("approval_reason") or "CrewAI 退款申请待审批",
                    "refund_amount": result.get("refund_amount"),
                    "model": state.get("model") or llm.model,
                })
            else:
                # 写意图未回传待审批记录 → fail-closed 转人工，绝不执行。
                out.update(_fail_closed(state, "crewai_write_no_approval_in_response",
                                        "CrewAI 写操作未返回待审批记录"))
        else:
            # 只读 / 升级意图 → 正常产生工具结果，走 generate_response。
            out.update({
                "needs_approval": False,
                "pending_action": None,
                "tool_results": [str(crew_result) if crew_result else "已处理"],
            })
        return out

    builder = StateGraph(AgentState)
    builder.add_node("classify_intent", nodes["classify_intent"])
    builder.add_node("route_by_intent", lambda state: {})  # 仅作为条件边锚点
    builder.add_node("crewai_refund_agent", crewai_refund_agent_node)
    builder.add_node("query_order", nodes["query_order"])
    builder.add_node("track_shipping", nodes["track_shipping"])
    builder.add_node("process_refund", nodes["process_refund"])
    builder.add_node("process_return", nodes["process_return"])
    builder.add_node("update_return_address", nodes["update_return_address"])
    builder.add_node("escalate_ticket", nodes["escalate_ticket"])
    builder.add_node("agentic_rag", rag_graph)
    builder.add_node("human_approval", approval_node)
    builder.add_node("execute_refund", nodes["execute_refund"])
    builder.add_node("execute_return", nodes["execute_return"])
    builder.add_node("execute_address_update", nodes["execute_address_update"])
    builder.add_node("generate_response", nodes["generate_response"])
    builder.add_node("handle_error", nodes["handle_error"])

    builder.add_edge(START, "classify_intent")
    builder.add_edge("classify_intent", "route_by_intent")
    builder.add_conditional_edges(
        "route_by_intent", crewai_route_condition,
        {
            "crewai_refund_agent": "crewai_refund_agent",
            "query_order": "query_order",
            "track_shipping": "track_shipping",
            "process_refund": "process_refund",
            "process_return": "process_return",
            "update_return_address": "update_return_address",
            "agentic_rag": "agentic_rag",
            "escalate_ticket": "escalate_ticket",
            "generate_response": "generate_response",
        },
    )

    # crewai 写结果后置：写且创建待审批 → 唯一 human_approval；失败 → handle_error；只读 → 回复。
    builder.add_conditional_edges(
        "crewai_refund_agent", crewai_result_condition,
        {"approve": "human_approval", "handle_error": "handle_error", "done": "generate_response"},
    )

    # 敏感操作一律进入唯一审批节点（无 direct 绕过路径）；仅当成功创建待审批时才进入，
    # 资格/参数/能力校验失败 fail-closed 转 handle_error（人工），不直接执行。
    builder.add_conditional_edges(
        "process_refund", write_approval_condition,
        {"approve": "human_approval", "handle_error": "handle_error"},
    )
    builder.add_conditional_edges(
        "process_return", write_approval_condition,
        {"approve": "human_approval", "handle_error": "handle_error"},
    )
    builder.add_conditional_edges(
        "update_return_address", write_approval_condition,
        {"approve": "human_approval", "handle_error": "handle_error"},
    )
    builder.add_conditional_edges(
        "human_approval", approval_result_condition,
        {
            "approved_refund": "execute_refund",
            "approved_return": "execute_return",
            "approved_address": "execute_address_update",
            "rejected": "handle_error",
        },
    )
    builder.add_edge("execute_refund", "generate_response")
    builder.add_edge("execute_return", "generate_response")
    builder.add_edge("execute_address_update", "generate_response")
    builder.add_edge("escalate_ticket", "generate_response")
    # 只读查询节点必须回边到 generate_response，否则图停在查询节点、无法产出回复。
    builder.add_edge("query_order", "generate_response")
    builder.add_edge("track_shipping", "generate_response")

    builder.add_conditional_edges(
        "agentic_rag", rag_result_condition,
        {"done": "generate_response", "error": "handle_error"},
    )
    builder.add_edge("handle_error", "generate_response")
    builder.add_edge("generate_response", END)

    return builder.compile(checkpointer=checkpointer)
