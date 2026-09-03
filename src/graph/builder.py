"""LangGraph 主图构建。

无 direct -> execute 绕过：process_refund / process_return / update_return_address
一律 add_edge 到唯一 human_approval，审批结果按 pending_action 单一路由到对应 execute_*。
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from src.graph.approval import make_approval_node
from src.graph.nodes import make_nodes
from src.graph.rag import make_rag_graph
from src.graph.state import AgentState


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


def build_graph(llm, store, checkpointer, retriever=None, adapter=None, conf_threshold=0.7):
    """构建并编译主图（checkpointer 注入以支持中断恢复与审批续跑）。"""
    nodes = make_nodes(llm, store, adapter=adapter, conf_threshold=conf_threshold)
    rag_graph = make_rag_graph(llm, retriever=retriever)
    approval_node = make_approval_node(store)

    builder = StateGraph(AgentState)
    builder.add_node("classify_intent", nodes["classify_intent"])
    builder.add_node("route_by_intent", lambda state: {})  # 仅作为条件边锚点
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
        "route_by_intent", route_condition,
        {
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
