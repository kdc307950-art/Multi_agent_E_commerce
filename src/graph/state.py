"""AgentState 状态契约（主图与 RAG 子图共用）。

约束（冻结，见《项目说明书》§2.0 / 《项目架构说明书》§2.1）：
- tenant_id 由服务端认证上下文注入，节点/模型/客户端均不得覆盖。
- rag_answer 仅供本轮幻觉检测；不得回退读取 final_response。
- reason 只存机器可读失败/回退代码；审批给人看的业务摘要放 approval_reason。
- 敏感写操作（refund/return_request/return_address）无 direct -> execute 绕过路径。
"""
from __future__ import annotations

from typing import Annotated, Literal, Optional, Required

from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


def last_message_text(messages) -> str:
    """提取最后一条消息的文本。

    使用 add_messages reducer 后，消息可能是 LangChain 的 HumanMessage/AIMessage 对象
    （.content）或 dict（["content"]），需统一处理。
    """
    if not messages:
        return ""
    msg = messages[-1]
    if isinstance(msg, dict):
        content = msg.get("content", "")
    else:
        content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # content blocks：拼接文本片段。
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(str(block.get("text", "")))
        return "\n".join(parts)
    return str(content)


class AgentState(TypedDict, total=False):
    # 对话历史（追加语义）
    messages: Required[Annotated[list, add_messages]]
    # 服务端注入的会话与请求上下文（客户端不可覆盖）
    tenant_id: Required[str]
    user_id: Required[str]
    role: Optional[str]               # 服务端认证后的租户成员角色（customer/agent/admin/approver）
    thread_id: Optional[str]              # 服务端签发的会话线程
    client_request_id: Optional[str]      # 服务端接收的请求去重 ID（start 提供）
    order_id: Optional[str]

    # 意图与路由
    intent: Optional[Literal[
        "order", "shipping", "refund", "return_request",
        "return_address", "policy", "complaint", "other"
    ]]
    confidence: Optional[float]

    # 工具调用
    tool_calls: list[dict]
    tool_results: list[dict]

    # RAG
    retrieved_docs: list[dict]
    rewritten_query: Optional[str]
    rag_answer: Optional[str]             # 本轮 RAG 草稿；仅供幻觉检测
    hallucination_check: Optional[dict]
    needs_self_correction: bool
    correction_strategy: Optional[str]
    falls_to_error: bool
    reason: Optional[str]                 # 机器可读失败/回退代码
    rag_attempts: int
    rag_max_retries: int
    hallucination_attempts: int
    hallucination_max_retries: int

    # 审批 / 业务执行
    pending_action: Optional[Literal["refund", "return_request", "return_address"]]
    approval_id: Optional[str]
    approval_reason: Optional[str]        # 给审批人的业务摘要
    operation_id: Optional[str]           # 业务操作唯一标识（幂等键）
    needs_approval: bool
    approval_status: Optional[Literal["pending", "approved", "rejected"]]
    approval_payload: Optional[dict]
    refund_amount: Optional[float]
    model: Optional[str]                  # 能力矩阵/门控用

    # 执行面（approval 后 execute 节点的细粒度状态，见 src/execution）：
    # 幂等锚点 execution_id + 状态机 execution_status（shadow/live/compensated/reconciled 等）。
    execution_id: Optional[str]
    execution_status: Optional[str]       # ExecutionStatus.value（pending_submit/submitted/confirmed/...）
    receipt: Optional[dict]               # 模拟/真实回执（含 external_txn_id）
    human_handoff: bool                   # 执行异常 → 转人工对账

    # 最终输出 / 错误
    final_response: Optional[str]
    error: Optional[str]
