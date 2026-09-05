## 1. 项目结构

```text
after-sales-agent/
├── README.md
├── requirements.txt
├── .env.example
├── docker-compose.yml
│
├── src/
│   ├── __init__.py
│   ├── main.py                 # FastAPI 入口
│   ├── config.py               # 配置管理
│   │
│   ├── graph/                  # LangGraph 主图
│   │   ├── __init__.py
│   │   ├── state.py            # 状态定义
│   │   ├── nodes.py            # 所有节点函数
│   │   ├── edges.py            # 条件路由
│   │   ├── graph.py            # 图构建与编译
│   │   └── checkpointer.py     # 检查点配置
│   │
│   ├── crews/                  # CrewAI 子智能体
│   │   ├── __init__.py
│   │   ├── order_crew.py       # 订单查询专家
│   │   ├── shipping_crew.py    # 物流追踪专家
│   │   └── refund_crew.py      # 退款处理专家
│   │
│   ├── operations/             # 业务执行（审批后真正写库/提交）
│   │   ├── __init__.py
│   │   ├── refund.py           # execute_refund（写库标记 + 可替换提交接口）
│   │   ├── return_request.py   # execute_return（退货申请写库）
│   │   └── address.py          # execute_address_update（改址写库）
│   │
│   ├── rag/                    # Agentic RAG 模块
│   │   ├── __init__.py
│   │   ├── retriever.py        # 混合检索
│   │   ├── rewrite.py          # 查询改写
│   │   ├── grader.py           # 相关性评分
│   │   ├── hallucination.py    # 幻觉检测
│   │   └── ingest.py           # 知识库注入
│   │
│   ├── tools/                  # 工具函数
│   │   ├── __init__.py
│   │   ├── db_tools.py         # 数据库操作
│   │   ├── api_tools.py        # API 调用（模拟/可替换）
│   │   └── mock_data.py        # 模拟数据
│   │
│   ├── observability/          # 可观测性
│   │   ├── __init__.py
│   │   ├── tracer.py           # 自托管 Langfuse 配置
│   │   ├── metrics.py          # 指标收集
│   │   └── evaluator.py        # 自动化评估
│   │
│   ├── auth/                   # 鉴权与会话
│   │   ├── __init__.py
│   │   ├── dependencies.py     # get_tenant_context / validate_session_owner
│   │   ├── tenant_store.py     # 租户、成员关系、角色与状态校验
│   │   └── session_store.py    # 租户作用域内的会话元数据表读写
│   │
│   ├── tasks/                  # Celery 后台任务
│   │   └── __init__.py
│   │
│   └── ui/                     # Web UI 入口（前端为 React + Next.js）
│       └── __init__.py
│
├── frontend/                   # React + Next.js 前端（见《生产基线与验收测试》）
│   └── app/
├── tests/                      # 测试
│   ├── test_graph.py
│   ├── test_crews.py
│   ├── test_rag.py
│   ├── test_auth.py
│   ├── test_tenant_isolation.py
│   └── fixtures/               # 测试数据
│       └── test_cases.json     # 评估测试集(≥50条)
│
├── data/                       # 数据文件
│   ├── knowledge_base/         # RAG 知识库文档
│   ├── checkpoints/            # SQLite 检查点
│   └── mock_db/                # 模拟数据库
│
└── docs/                       # 文档
    ├── architecture.md
    ├── api_reference.md
    └── deployment.md
```

## 2. 核心模块说明

### 2.0 AgentState 状态契约（主图与 RAG 共用）

`AgentState` 是主图、Agentic RAG 子图、人工审批恢复和错误处理之间的显式接口。以下字段是设计基线；实际代码必须在 `src/graph/state.py` 中以 `TypedDict`（或等效 schema）声明，节点不得依赖未声明的临时键。

| 字段 | 类型 | 归属/用途 | 约束 |
| :-- | :-- | :-- | :-- |
| `tenant_id` | `str` | 服务端租户作用域 | 由认证上下文注入，不接受模型/客户端覆盖 |
| `user_id` / `order_id` | `str` / `Optional[str]` | 当前主体与订单 | 查询前同时匹配 `tenant_id` |
| `thread_id` | `Optional[str]` | 会话线程（服务端签发） | 服务端注入；HTTP 层校验归属，不用于推断身份 |
| `client_request_id` | `Optional[str]` | start 请求去重 ID | 服务端注入；用于敏感写幂等键与 start 原子去重 |
| `messages` | `list[dict]` | 对话历史 | 采用追加语义，不覆盖历史 |
| `intent` / `confidence` | `Optional[str]` / `Optional[float]` | 意图分类 | 退款、退货申请、退货地址变更保持独立 |
| `retrieved_docs` / `rewritten_query` | `list[dict]` / `Optional[str]` | RAG 检索中间态 | 文档召回必须带租户过滤 |
| `rag_answer` | `Optional[str]` | **本轮 RAG 草稿** | 仅供本轮幻觉检测；不得回退读取 `final_response` |
| `hallucination_check` | `Optional[dict]` | 幻觉检测结果 | 只接受 `{faithful: bool, issues: list[str]}`；草稿缺失或 schema 非法均 fail-closed |
| `rag_attempts` / `rag_max_retries` | `int` / `int` | 相关性自纠正次数/上限 | 超限进入错误处理，不无限循环 |
| `hallucination_attempts` / `hallucination_max_retries` | `int` / `int` | 幻觉重生成次数/上限 | 每次生成覆盖当前 `rag_answer` 并清空旧检测结果 |
| `needs_self_correction` / `correction_strategy` | `bool` / `Optional[str]` | RAG 自纠正路由 | 仅允许预定义策略 |
| `falls_to_error` / `reason` | `bool` / `Optional[str]` | 统一错误/回退路由 | `reason` 使用机器可读代码 |
| `pending_action` / `approval_id` / `approval_status` / `approval_reason` | 业务枚举 / `Optional[str]` / 枚举 / `Optional[str]` | 人工审批恢复 | `approval_reason` 是给审批人的业务摘要，不复用机器可读失败 `reason`；敏感写操作无直达执行路径 |
| `operation_id` | `Optional[str]` | 幂等键 | 重试不改变同一业务操作 ID |
| `final_response` / `error` | `Optional[str]` / `Optional[str]` | 主图最终输出 | 仅由主图最终回复/错误出口写入 |

> **RAG 状态规则**：每次进入 RAG 子图必须先执行 `initialize_rag_run`，清空上轮 checkpoint 遗留的查询、文档、草稿、检测和计数状态，并设置本轮上限。`generate_rag_answer` 必须返回 `rag_answer`；`check_hallucination` 只读取本轮 `rag_answer`，且其 JSON 只接受 `{faithful: bool, issues: list[str]}`。字段缺失或检测结果 schema 非法时，返回失败检测、设置 `falls_to_error=True` 和机器可读 `reason`，不得读取旧 checkpoint 或主图的 `final_response`。只有 `faithful is True` 的草稿可提升为 `final_response`。

### 2.1 主图流程 (graph.py)

```python
from langgraph.graph import StateGraph, START, END, Command
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import interrupt
from src.graph.state import AgentState
from src.graph.nodes import *
from src.graph.edges import *


def build_graph():
    builder = StateGraph(AgentState)

    # LangGraph 主控节点
    builder.add_node("classify_intent", classify_intent_node)
    builder.add_node("route_by_intent", route_by_intent_node)

    # 工具 / 子智能体节点
    builder.add_node("query_order", run_order_crew)
    builder.add_node("track_shipping", run_shipping_crew)
    builder.add_node("process_refund", run_refund_crew)
    builder.add_node("process_return", run_return_crew)            # 退货申请（独立）
    builder.add_node("update_return_address", run_address_crew)    # 改址（独立）
    builder.add_node("escalate_ticket", escalate_ticket_node)
    builder.add_node("agentic_rag", run_agentic_rag_subgraph)

    # 唯一的人工审批中断节点（按 pending_action 区分执行分支）
    builder.add_node("human_approval", approval_node)

    # 审批后的业务执行节点（真正写库 / 提交，均幂等）
    builder.add_node("execute_refund", execute_refund_node)
    builder.add_node("execute_return", execute_return_node)
    builder.add_node("execute_address_update", execute_address_node)

    builder.add_node("generate_response", generate_response_node)
    builder.add_node("handle_error", handle_error_node)

    # 入口
    builder.add_edge(START, "classify_intent")
    builder.add_edge("classify_intent", "route_by_intent")

    # 意图路由：order / shipping / return_request / refund / return_address / policy / complaint / other
    builder.add_conditional_edges(
        "route_by_intent",
        route_condition,
        {
            "order": "query_order",
            "shipping": "track_shipping",
            "refund": "process_refund",
            "return_request": "process_return",
            "return_address": "update_return_address",
            "policy": "agentic_rag",
            "complaint": "escalate_ticket",
            "other": "generate_response",
        },
    )

    # 敏感操作一律进入唯一审批节点（无 direct 绕过路径）
    builder.add_edge("process_refund", "human_approval")
    builder.add_edge("process_return", "human_approval")
    builder.add_edge("update_return_address", "human_approval")

    # 审批结果按 pending_action 单一路由，approved → 对应执行节点
    builder.add_conditional_edges(
        "human_approval",
        approval_result_condition,
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

    # RAG 子图内部拥有唯一的自纠正循环；主图只接收成功或安全失败结果。
    builder.add_conditional_edges(
        "agentic_rag",
        rag_result_condition,
        {"done": "generate_response", "error": "handle_error"},
    )
    # 错误处理统一出口
    builder.add_edge("handle_error", "generate_response")
    builder.add_edge("generate_response", END)

    # 原型用 SQLite；生产用 TenantScopedCheckpointer（基于 AsyncPostgresSaver，见《生产基线与验收测试》）
    checkpointer = SqliteSaver.from_conn_string("checkpoints.db")
    return builder.compile(checkpointer=checkpointer)
```

### 2.2 意图分类 (nodes.py)

```python
def classify_intent_node(state: AgentState) -> dict:
    """LLM 识别用户意图；返回独立意图（退货申请 / 退款 / 改址分开）。"""
    user_message = state["messages"][-1]["content"]
    prompt = f"""
    分析以下用户消息的意图，从以下类别中选择：
    - return_request: 退货申请（申请发起退货）
    - refund: 退款申请
    - shipping: 物流查询
    - policy: 政策咨询
    - complaint: 投诉
    - return_address: 退货地址变更（已有退货，要改收货/退货地址）
    - order: 订单信息查询
    - other: 其他

    消息：{user_message}
    返回JSON格式：{{"intent": "类别", "confidence": 0.0-1.0, "order_id": "若有则提取"}}
    """
    response = llm.invoke(prompt)
    result = json.loads(response.content)
    return {
        "intent": result["intent"],
        "confidence": result["confidence"],
        "order_id": result.get("order_id"),
    }
```

### 2.3 Agentic RAG 子图 (rag/subgraph.py)

```python
def build_rag_subgraph():
    """构建 Agentic RAG 子图。"""
    rag_builder = StateGraph(AgentState)
    rag_builder.add_node("initialize_rag_run", initialize_rag_run)
    rag_builder.add_node("rewrite_query", rewrite_query_node)
    rag_builder.add_node("route_retrieval", route_retrieval_node)
    rag_builder.add_node("hybrid_search", hybrid_search_node)
    rag_builder.add_node("grade_relevance", grade_relevance)
    rag_builder.add_node("generate_answer", generate_rag_answer)
    rag_builder.add_node("check_hallucination", check_hallucination)

    # 初始化只在本轮子图入口运行，不能放进重试环，否则计数会被重置。
    rag_builder.add_edge(START, "initialize_rag_run")
    rag_builder.add_edge("initialize_rag_run", "rewrite_query")
    rag_builder.add_edge("rewrite_query", "route_retrieval")
    rag_builder.add_edge("route_retrieval", "hybrid_search")
    rag_builder.add_edge("hybrid_search", "grade_relevance")

    # 自纠正循环
    rag_builder.add_conditional_edges(
        "grade_relevance",
        should_retry,
        {"retry": "rewrite_query", "proceed": "generate_answer", "error": END},
    )
    rag_builder.add_edge("generate_answer", "check_hallucination")
    rag_builder.add_conditional_edges(
        "check_hallucination",
        hallucination_condition,
        {"regenerate": "generate_answer", "done": END, "error": END},
    )
    return rag_builder.compile()


def should_retry(state: AgentState) -> str:
    """错误优先；只有相关性低且仍有预算时才改写查询。"""
    if state.get("falls_to_error"):
        return "error"
    if (state.get("needs_self_correction")
            and state.get("correction_strategy") == "rewrite_and_retry"):
        return "retry"
    return "proceed"


def hallucination_condition(state: AgentState) -> str:
    """错误优先；只有当前草稿未通过且仍有预算时才重新生成。"""
    if state.get("falls_to_error"):
        return "error"
    if (state.get("needs_self_correction")
            and state.get("correction_strategy") == "regenerate_from_sources"):
        return "regenerate"
    return "done"


def rag_result_condition(state: AgentState) -> str:
    """把子图的 fail-closed 状态显式冒泡给主图错误出口。"""
    return "error" if state.get("falls_to_error") else "done"
```

> **子图出站规则**：`falls_to_error=True` 必须优先于任何重试策略，经 `rag_result_condition` 进入主图 `handle_error`；子图内是唯一允许的 RAG 重试所有者，主图不得再次回跳 `agentic_rag`。这样既避免双层循环重置计数，也让空检索、相关性重试耗尽、草稿缺失、幻觉检查解析/调用失败和幻觉重试耗尽全部安全退出。

## 3. 数据模型

### 3.1 模拟订单数据

```python
# data/mock_db/orders.json
{
    "orders": [
        {
            "order_id": "ORD-001",
            "user_id": "USER-001",
            "status": "delivered",
            "total_amount": 299.00,
            "items": [
                {"name": "无线耳机", "sku": "SKU-001", "price": 299.00, "quantity": 1}
            ],
            "shipping": {
                "carrier": "SF Express",
                "tracking_no": "SF1234567890",
                "status": "delivered",
                "estimated_delivery": "2026-01-15"
            },
            "created_at": "2026-01-01T10:00:00",
            "delivered_at": "2026-01-15T14:30:00"
        }
    ]
}
```

### 3.2 RAG 知识库文档

```text
data/knowledge_base/
├── return_policy.md      # 退货政策
├── refund_policy.md      # 退款政策
├── shipping_policy.md    # 物流政策
├── faq.md                # 常见问题
└── complaint_process.md  # 投诉处理流程
```

## 4. API 接口

### 4.1 鉴权与会话归属

```python
# src/auth/dependencies.py
from fastapi import Depends, HTTPException, status


async def get_tenant_context(authorization: str = Header(...)) -> TenantContext:
    """仅解析活跃租户成员；不信任客户端 tenant_id/user_id。"""
    principal = await verify_token(authorization)
    return await tenant_context_store.resolve_active_membership(principal)


async def validate_session_owner(thread_id: str, ctx: TenantContext) -> Session:
    """校验租户、用户/角色与 thread 归属；不从 thread_id 解析身份做鉴权。"""
    session = await session_store.get_by_thread_id(tenant_id=ctx.tenant_id, thread_id=thread_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session["tenant_id"] != ctx.tenant_id:
        raise HTTPException(status_code=403, detail="无权访问该会话")
    if session["user_id"] != ctx.user_id and ctx.role not in {"agent", "admin", "approver"}:
        raise HTTPException(status_code=403, detail="无权访问该会话")
    return session
```

> `TenantContext.role` 只可能是 `customer`、`agent`、`admin`、`approver`。`platform_admin` 通过独立的 `PlatformOperationContext` 授权，只能进入显式目标租户、最小范围、二次确认和审计齐备的平台运维/合规接口；它不能替代租户成员身份，也不能访问 `/api/chat`、`/api/approvals/{approval_id}/decision` 或 `execute_*` 路径。

### 4.2 会话创建

```python
# src/api/routes.py
from datetime import datetime, timedelta, timezone

@router.post("/api/sessions", response_model=NewSessionResponse, status_code=201)
async def create_session(ctx: TenantContext = Depends(get_tenant_context)):
    """后端签发 thread_id，并与 checkpoint scope 原子创建。"""
    thread_id = generate_thread_id()          # 不透明 UUID，不嵌入 user_id
    now = datetime.now(timezone.utc)
    async with tenant_transaction(ctx) as tx:
        await session_store.create({
            "tenant_id": ctx.tenant_id,
            "thread_id": thread_id,
            "user_id": ctx.user_id,
            "created_at": now,
            "expires_at": now + timedelta(days=7),
            "status": "active",
        }, tx=tx)
        await checkpoint_scope_repo.create_active(
            tenant_id=ctx.tenant_id,
            thread_id=thread_id,
            expires_at=now + timedelta(days=7),
            tx=tx,
        )
    return NewSessionResponse(thread_id=thread_id, session_id=thread_id)
```

### 4.3 对话与审批

```python
# src/main.py
# 唯一 AI 流：只产生六种冻结的 SSE 事件，不返回 JSON ChatResponse。
from typing import Annotated
from fastapi import Body, Header, HTTPException
from fastapi.responses import StreamingResponse

# ChatStart/ChatResume 分别将 mode 固定为 start/resume；其字段见冻结 API 契约。
ChatRequest = Annotated[ChatStart | ChatResume, Body(discriminator="mode")]

@app.post("/api/chat")
async def chat(
    request: ChatRequest,
    ctx: TenantContext = Depends(get_tenant_context),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    """start 创建/附着流；resume 仅重放持久化事件，绝不重跑图或工具。"""
    if request.mode == "resume":
        if not last_event_id:
            raise HTTPException(status_code=422, detail="Last-Event-ID is required")
        stream = await stream_store.authorize_resume(ctx, request.stream_id, last_event_id)
        return StreamingResponse(
            stream_store.replay_after(stream, last_event_id),
            media_type="text/event-stream",
        )

    await validate_session_owner(request.thread_id, ctx)
    stream = await stream_store.create_or_get_start(
        ctx, thread_id=request.thread_id,
        client_request_id=request.client_request_id,
    )
    # ContextVar 必须覆盖整个异步迭代期；不能在返回 StreamingResponse 前就退出作用域。
    async def start_event_stream():
        async with checkpoint_request_scope(ctx, request.thread_id, source="request"):
            async for frame in stream_events_for_start(stream, ctx, request):
                yield frame

    return StreamingResponse(start_event_stream(), media_type="text/event-stream")


@app.post("/api/approvals/{approval_id}/decision")
async def decide_approval(
    approval_id: str,
    request: ApprovalDecisionRequest,
    ctx: TenantContext = Depends(get_tenant_context),
):
    """审批 REST 只恢复既有待审批操作；返回权威 operation_id，保持幂等。"""
    require_role(ctx, {"admin", "approver"})
    approval = await authorize_approval(ctx, approval_id)
    await validate_session_owner(approval.thread_id, ctx)
    require_confirmation(request)
    config = {"configurable": {"thread_id": approval.thread_id, "tenant_id": ctx.tenant_id}}
    async with checkpoint_request_scope(ctx, approval.thread_id, source="approval_resume"):
        await graph.ainvoke(
            Command(resume={
                "approved": request.approved,
                "feedback": request.feedback,
                "approver": ctx.user_id,
            }),
            config=config,
        )
    return await operation_store.decision_result(ctx, approval.operation_id)
```

> **冻结契约**：`POST /api/chat` 只接受 `{mode:"start", thread_id, client_request_id, message}` 或 `{mode:"resume", stream_id}`，始终返回 `text/event-stream`。帧的 `event` 仅允许 `accepted`、`token`、`node`、`approval_required`、`done`、`error`；审批唯一入口为 `POST /api/approvals/{approval_id}/decision`。审批基于 **approval_id / operation_id** 定位并同时校验 `tenant_id`、订单、会话和审批人授权范围；审批通过后进入唯一 `execute_*` 执行节点，绝无 `direct -> execute` 绕过路径。
