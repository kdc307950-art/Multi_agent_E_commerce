"""Agentic RAG 子图。

设计要点（见《项目架构说明书》§4）：
- 子图内部是唯一允许的 RAG 重试所有者；主图不得再次回跳 agentic_rag。
- 只读取本轮 rag_answer；不得读取旧 checkpoint 的 final_response。
- 检索/相关性为空、重试耗尽、草稿缺失、幻觉检测失败或重试耗尽一律 fail-closed
  设 falls_to_error=True → 主图 rag_result_condition 进入 handle_error。
- 幻觉检测 JSON 只接受 {'faithful': bool, 'issues': list[str]}，schema 非法即失败。
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from src.graph.state import AgentState, last_message_text
from src.llm.base import LLMError
from src.retrieval import DataSourceRetriever, Retriever
from src.tools.data_source import MockBusinessDataSource

# 阈值
RELEVANCE_THRESHOLD = 7
RAG_MAX_RETRIES = 3
HALLUCINATION_MAX_RETRIES = 2


def make_rag_graph(llm, retriever: Retriever | None = None):
    """构建 RAG 子图（无独立 checkpointer，作为主图的一个节点使用）。

    retriever 缺省为基于受控业务数据源的 `DataSourceRetriever`（Mock 数据源，租户作用域），
    不再直接读 `mock_data.KNOWLEDGE_BASE`。生产路径由 `build_retriever(settings)` 注入
    DataSourceRetriever（postgres 数据源）。任何检索/模型异常一律 fail-closed
    设 falls_to_error=True → 主图 handle_error 转人工。
    """
    retriever = retriever or DataSourceRetriever(MockBusinessDataSource())

    def initialize_rag_run(state: AgentState) -> dict:
        return {
            "rewritten_query": None,
            "retrieved_docs": [],
            "rag_answer": None,
            "hallucination_check": None,
            "needs_self_correction": False,
            "correction_strategy": None,
            "falls_to_error": False,
            "reason": None,
            "rag_attempts": 0,
            "rag_max_retries": RAG_MAX_RETRIES,
            "hallucination_attempts": 0,
            "hallucination_max_retries": HALLUCINATION_MAX_RETRIES,
        }

    def rewrite_query(state: AgentState) -> dict:
        original = last_message_text(state["messages"])
        try:
            rewritten = llm.rewrite_query(original)
        except LLMError:
            return {"rewritten_query": None, "falls_to_error": True,
                    "reason": "query_rewrite_failed"}
        return {"rewritten_query": rewritten}

    def route_retrieval(state: AgentState) -> dict:
        return {}

    def hybrid_search(state: AgentState) -> dict:
        tenant_id = state.get("tenant_id")
        query = state.get("rewritten_query") or last_message_text(state["messages"])
        if not tenant_id:
            return {"retrieved_docs": [], "falls_to_error": True,
                    "reason": "missing_tenant_context"}
        try:
            docs = retriever.search(tenant_id, query)
        except Exception:
            # 检索组件异常（如 Milvus 不可用）→ fail-closed 转人工。
            return {"retrieved_docs": [], "falls_to_error": True,
                    "reason": "retrieval_unavailable"}
        return {"retrieved_docs": docs}

    def grade_relevance(state: AgentState) -> dict:
        # 前置失败（如查询改写失败）直接短路为 error。
        if state.get("falls_to_error"):
            return {"needs_self_correction": False, "correction_strategy": None,
                    "falls_to_error": True, "reason": state.get("reason")}
        docs = state.get("retrieved_docs") or []
        query = state.get("rewritten_query", "") or last_message_text(state["messages"])
        if not docs:
            return {"needs_self_correction": False, "correction_strategy": None,
                    "falls_to_error": True, "reason": "no_relevant_documents"}
        scores = [llm.grade_relevance(query, d) for d in docs]
        max_score = max(scores)
        attempts = state.get("rag_attempts", 0)
        if max_score < RELEVANCE_THRESHOLD and attempts < state.get("rag_max_retries", RAG_MAX_RETRIES):
            return {"rag_attempts": attempts + 1, "needs_self_correction": True,
                    "correction_strategy": "rewrite_and_retry", "falls_to_error": False,
                    "reason": "low_relevance"}
        if max_score < RELEVANCE_THRESHOLD:
            return {"needs_self_correction": False, "correction_strategy": None,
                    "falls_to_error": True, "reason": "relevance_retry_exhausted"}
        return {"needs_self_correction": False, "correction_strategy": None,
                "falls_to_error": False, "reason": None}

    def generate_answer(state: AgentState) -> dict:
        docs = state.get("retrieved_docs") or []
        query = state.get("rewritten_query") or last_message_text(state["messages"])
        try:
            answer = llm.generate_rag_answer(query, docs)
        except LLMError:
            return {"rag_answer": None, "hallucination_check": None,
                    "needs_self_correction": False, "correction_strategy": None,
                    "falls_to_error": True, "reason": "answer_generation_failed"}
        return {"rag_answer": answer, "hallucination_check": None,
                "needs_self_correction": False, "correction_strategy": None,
                "falls_to_error": False, "reason": None}

    def check_hallucination(state: AgentState) -> dict:
        answer = state.get("rag_answer")
        docs = state.get("retrieved_docs") or []
        if not answer:
            return {"hallucination_check": {"faithful": False, "issues": ["missing_rag_answer"]},
                    "falls_to_error": True, "needs_self_correction": False,
                    "correction_strategy": None, "reason": "missing_rag_answer"}
        try:
            check = llm.check_hallucination(answer, docs)
        except LLMError:
            # 模型异常或输出非法 → fail-closed。
            return {"hallucination_check": {"faithful": False, "issues": ["hallucination_check_error"]},
                    "falls_to_error": True, "needs_self_correction": False,
                    "correction_strategy": None, "reason": "hallucination_check_invalid_output"}
        # schema 校验：非法即 fail-closed。
        if not (isinstance(check, dict)
                and isinstance(check.get("faithful"), bool)
                and isinstance(check.get("issues"), list)):
            return {"hallucination_check": {"faithful": False, "issues": ["invalid_hallucination_check"]},
                    "falls_to_error": True, "needs_self_correction": False,
                    "correction_strategy": None, "reason": "hallucination_check_invalid_output"}
        attempts = state.get("hallucination_attempts", 0)
        if not check.get("faithful"):
            if attempts < state.get("hallucination_max_retries", HALLUCINATION_MAX_RETRIES):
                return {"hallucination_check": check, "hallucination_attempts": attempts + 1,
                        "needs_self_correction": True, "correction_strategy": "regenerate_from_sources",
                        "falls_to_error": False, "reason": "unfaithful_rag_draft"}
            return {"hallucination_check": check, "falls_to_error": True,
                    "needs_self_correction": False, "correction_strategy": None,
                    "reason": "hallucination_retry_exhausted"}
        return {"hallucination_check": check, "needs_self_correction": False,
                "correction_strategy": None, "falls_to_error": False, "reason": None}

    def should_retry(state: AgentState) -> str:
        if state.get("falls_to_error"):
            return "error"
        if state.get("needs_self_correction") and state.get("correction_strategy") == "rewrite_and_retry":
            return "retry"
        return "proceed"

    def hallucination_condition(state: AgentState) -> str:
        if state.get("falls_to_error"):
            return "error"
        if state.get("needs_self_correction") and state.get("correction_strategy") == "regenerate_from_sources":
            return "regenerate"
        return "done"

    builder = StateGraph(AgentState)
    builder.add_node("initialize_rag_run", initialize_rag_run)
    builder.add_node("rewrite_query", rewrite_query)
    builder.add_node("route_retrieval", route_retrieval)
    builder.add_node("hybrid_search", hybrid_search)
    builder.add_node("grade_relevance", grade_relevance)
    builder.add_node("generate_answer", generate_answer)
    builder.add_node("check_hallucination", check_hallucination)

    builder.add_edge(START, "initialize_rag_run")
    builder.add_edge("initialize_rag_run", "rewrite_query")
    builder.add_edge("rewrite_query", "route_retrieval")
    builder.add_edge("route_retrieval", "hybrid_search")
    builder.add_edge("hybrid_search", "grade_relevance")
    builder.add_conditional_edges("grade_relevance", should_retry,
                                  {"retry": "rewrite_query", "proceed": "generate_answer", "error": END})
    builder.add_edge("generate_answer", "check_hallucination")
    builder.add_conditional_edges("check_hallucination", hallucination_condition,
                                  {"regenerate": "generate_answer", "done": END, "error": END})
    return builder.compile()
