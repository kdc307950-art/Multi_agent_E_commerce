## 1. 整体架构

> 本节为 **Agent 内部流程架构**（设计态）；生产部署（前后端分离、React 前端、Neo4j、自托管可观测）见《生产基线与验收测试》。

系统采用**四层嵌套架构**，LangGraph 作为统一编排主体：

```text
┌─────────────────────────────────────────────────────────────────┐
│                      用户交互层 (React + Next.js)                │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│              Layer 1: LangGraph 主控层 (状态机 + 流程控制)       │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐       │
│  │ 意图识别  │→│  路由决策 │→│ 审批/执行 │→│ 回复生成 │       │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘       │
│       ↑             ↑             ↑             ↑              │
│  [检查点]      [条件边]     [中断节点]    [状态聚合]            │
└─────────────────────────────────────────────────────────────────┘
          │             │             │
          ▼             ▼             ▼
┌─────────────────┐ ┌──────────────┐ ┌─────────────────────────┐
│  Layer 2:       │ │  Layer 3:    │ │  Layer 4:               │
│  CrewAI子智能体层│ │  Agentic RAG │ │  可观测性与评估层        │
│  ┌───────────┐  │ │  ┌────────┐  │ │  ┌──────────────────┐   │
│  │订单查询专家│  │ │  │查询改写│  │ │  │ 自托管Langfuse追踪│   │
│  ├───────────┤  │ │  ├────────┤  │ │  ├──────────────────┤   │
│  │物流追踪专家│  │ │  │混合检索│  │ │  │ 核心业务指标大盘  │   │
│  ├───────────┤  │ │  ├────────┤  │ │  ├──────────────────┤   │
│  │退款处理专家│  │ │  │幻觉检测│  │ │  │ 自动化评估测试    │   │
│  └───────────┘  │ │  └────────┘  │ │  └──────────────────┘   │
└─────────────────┘ └──────────────┘ └─────────────────────────┘
```

## 2. Layer 1: LangGraph 主控层

### 2.1 状态定义 (State)

```python
from typing import Annotated, TypedDict, List, Optional, Literal, Required
from langgraph.graph.message import add_messages


class AgentState(TypedDict, total=False):
    # 对话历史（用聚合器追加，避免覆盖历史）
    messages: Required[Annotated[List[dict], add_messages]]
    tenant_id: Required[str]       # 服务端认证后的租户上下文，禁止由模型/客户端指定
    user_id: Required[str]
    thread_id: Optional[str]       # 服务端签发的会话线程；HTTP 层校验归属
    client_request_id: Optional[str]  # start 请求去重 ID，用于敏感写幂等与 start 去重
    order_id: Optional[str]

    # 意图与路由
    intent: Optional[Literal[
        "order", "shipping", "refund", "return_request",
        "return_address", "policy", "complaint", "other"
    ]]
    confidence: Optional[float]

    # 工具调用
    tool_calls: List[dict]
    tool_results: List[dict]

    # RAG
    retrieved_docs: List[dict]
    rewritten_query: Optional[str]
    rag_answer: Optional[str]         # 当前 RAG 子图生成的答案草稿；仅供幻觉检测使用
    hallucination_check: Optional[dict]
    needs_self_correction: bool
    correction_strategy: Optional[str]
    falls_to_error: bool
    reason: Optional[str]             # 机器可读的失败/回退原因
    rag_attempts: int
    rag_max_retries: int
    hallucination_attempts: int
    hallucination_max_retries: int

    # 审批 / 业务执行
    pending_action: Optional[Literal["refund", "return_request", "return_address"]]
    approval_id: Optional[str]        # 审批单唯一标识
    approval_reason: Optional[str]    # 业务/资格摘要，不能复用机器可读的失败 reason
    operation_id: Optional[str]       # 业务操作唯一标识（幂等键）
    needs_approval: bool
    approval_status: Optional[Literal["pending", "approved", "rejected"]]
    approval_payload: Optional[dict]
    refund_amount: Optional[float]
    model: Optional[str]              # 能力矩阵/门控用

    # 最终输出
    final_response: Optional[str]
    error: Optional[str]
```

> **状态契约约束**：RAG 子图只能读取本轮 `rag_answer`，不得读取 `final_response` 作为草稿或检测输入；`final_response` 只由主图的 `generate_response` 节点写入。`needs_self_correction`、`correction_strategy`、`falls_to_error`、`hallucination_check` 与 `reason` 均属于显式状态契约，不能仅靠 TypedDict 运行时“多写字段”来约定。`reason` 只存机器可读的失败/回退代码；审批给人看的业务摘要放在独立的 `approval_reason`，两者不得混用。

> **租户上下文规则**：`tenant_id` 在进入主图前由 `get_tenant_context` 解析并校验（认证主体、成员关系、租户状态、角色），之后作为不可变状态贯穿所有节点。`classify_intent` 只分类，`route_by_intent` 只路由；任一节点缺少租户上下文都必须进入 `handle_error`/人工升级，不得使用默认租户。

### 2.2 主图节点设计

| 节点名称 | 功能 | 类型 |
| :-- | :-- | :-- |
| `classify_intent` | 识别用户意图 | LLM 节点 |
| `route_by_intent` | 条件路由 | 条件边 |
| `query_order` | 调用 CrewAI 订单专家 | 工具节点 |
| `track_shipping` | 调用 CrewAI 物流专家 | 工具节点 |
| `process_refund` | 调用 CrewAI 退款专家 | 工具节点（触发审批） |
| `process_return` | 处理退货申请 | 工具节点（触发审批） |
| `update_return_address` | 处理退货地址变更 | 工具节点（触发审批） |
| `escalate_ticket` | 转人工/升级 | 工具节点 |
| `agentic_rag` | Agentic RAG 子图 | 子图节点 |
| `human_approval` | 唯一人工审批中断 | 中断节点 |
| `execute_refund` | 审批后写库标记 + 可替换提交接口（幂等） | 业务执行节点 |
| `execute_return` | 审批后写退货申请状态（幂等） | 业务执行节点 |
| `execute_address_update` | 审批后写退货地址（幂等） | 业务执行节点 |
| `generate_response` | 生成最终回复 | LLM 节点 |
| `handle_error` | 错误处理出口 | 函数节点 |

### 2.3 检查点与持久化

```python
from langgraph.checkpoint.sqlite import SqliteSaver

# 原型用 SQLite；生产用 TenantScopedCheckpointer（基于 AsyncPostgresSaver，见《生产基线与验收测试》）
checkpointer = SqliteSaver.from_conn_string("checkpoints.db")
graph = builder.compile(checkpointer=checkpointer)

# 每次 invoke 指定不透明 thread_id + 已验证租户上下文
config = {"configurable": {"thread_id": thread_id, "tenant_id": ctx.tenant_id}}
input_state = {"tenant_id": ctx.tenant_id, "user_id": ctx.user_id, "messages": messages}
result = graph.invoke(input_state, config=config)
```

生产 PostgreSQL 采用共享库 + `tenant_id` 联合索引/唯一约束，并启用 RLS 或等效 repository guard。`orders`、`tickets`、`approvals`、`refund_ops`、检查点和审计表不得存在无租户范围的读写路径。

### 2.4 唯一人工审批实现

```python
from langgraph.types import interrupt


def approval_node(state: AgentState):
    """唯一审批节点；按 pending_action 展示对应审批单。"""
    payload = {
        "tenant_id": state["tenant_id"],
        "approval_id": state["approval_id"],
        "pending_action": state["pending_action"],   # refund / return_request / return_address
        "order_id": state["order_id"],
        "amount": state.get("refund_amount"),
        "reason": state.get("approval_reason"),
        "question": "请审批此操作申请",
    }
    response = interrupt(payload)
    if response.get("approved"):
        return {"approval_status": "approved", "approval_payload": response}
    return {"approval_status": "rejected", "error": response.get("feedback")}
```

恢复执行：

```python
from langgraph.types import Command

graph.invoke(Command(resume={"approved": True}), config=config)
```

> **审批路由**：`approval_result_condition` 读取 `pending_action` 与 `approval_status`，决定进入 `execute_refund / execute_return / execute_address_update` 或 `handle_error`。全流程**无 direct 绕过**，退款/退货/改址一律人工审批。

## 3. Layer 2: CrewAI 子智能体层

### 3.1 设计原理

将 CrewAI 的 Agent、Task、Crew 实体**封装为 LangGraph 的节点**，实现模块化工作流与灵活编排。LangGraph 对全链路状态与分支可控、可观测；CrewAI 承担角色化子任务。**本项目不引用外部第三方"成功率/延迟倍率"结论作为自身达标承诺**。

### 3.2 三个专家 Agent

```python
from crewai import Agent, Task, Crew

order_analyst = Agent(
    role="订单查询专家",
    goal="准确查询订单状态、金额、商品信息",
    backstory="拥有多年电商后台经验的订单管理专家",
    tools=[query_order_db, parse_order_details],
    verbose=True,
    allow_delegation=False,
)

shipping_tracker = Agent(
    role="物流追踪专家",
    goal="提供准确的物流轨迹和预计送达时间",
    backstory="物流行业资深从业者，熟悉各大快递公司系统",
    tools=[track_logistics, parse_shipping_info],
    verbose=True,
    allow_delegation=False,
)

refund_specialist = Agent(
    role="退款处理专家",
    goal="高效处理退款申请，确保符合政策",
    backstory="售后部门资深专员，精通退款流程和政策",
    tools=[check_refund_eligibility, calculate_refund_amount],
    verbose=True,
    allow_delegation=False,
)
```

> **退货申请 / 改址业务**：由退款专家之外的新增业务节点处理（`process_return` / `update_return_address`），依赖业务工具而非单纯 LLM 生成，**一律触发人工审批**。

### 3.3 集成到 LangGraph

```python
def run_crew_node(state: AgentState, crew: Crew) -> dict:
    result = crew.kickoff(inputs=state)
    return {"tool_results": [{"crew_result": result.raw}]}
```

## 4. Layer 3: Agentic RAG 层

### 4.1 设计原理

Agentic RAG 融合**动态查询分析**和**自我纠错机制**：查询改写 → 检索 → 相关性评分 → 自纠正闭环 → 幻觉检测。针对具体实体查询使用直接区块检索，针对开放性概念问题使用分层检索。

### 4.2 RAG 子图节点

```text
用户问题
    ↓
┌─────────────────────────────────────────────┐
│            Agentic RAG 子图                  │
│  ┌──────────┐    ┌──────────┐    ┌────────┐ │
│  │查询改写   │───→│ 路由决策  │───→│ 检索   │ │
│  └──────────┘    └──────────┘    └────────┘ │
│        ↑              │              │       │
│        │              ▼              ▼       │
│  ┌──────────┐    ┌────────────────────────┐ │
│  │ 自纠正   │←───│ 相关性评分 (Grade)      │ │
│  └──────────┘    └────────────────────────┘ │
│        ↑              │                      │
│        └──────────────┘ (不相关则重试)       │
└─────────────────────────────────────────────┘
    ↓
最终答案
```

### 4.3 核心实现

```python
def initialize_rag_run(state: AgentState) -> dict:
    """每次进入子图都创建独立的 RAG 运行状态，避免 checkpoint 残留污染。"""
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
        "rag_max_retries": 3,
        "hallucination_attempts": 0,
        "hallucination_max_retries": 2,
    }


def rewrite_query(state: AgentState) -> dict:
    """使用 LLM 改写用户问题，优化检索效果。"""
    original = state["messages"][-1]["content"]
    rewritten = llm.invoke(f"将以下用户问题改写为更适合检索的查询语句：{original}")
    return {"rewritten_query": rewritten.content}


def grade_relevance(state: AgentState) -> dict:
    """评估检索结果相关性；空检索 / 解析容错 + 上限转错误，避免 max([]) 报错。"""
    docs = state.get("retrieved_docs") or []
    query = state.get("rewritten_query", "")
    if not docs:
        return {
            "needs_self_correction": False,
            "correction_strategy": None,
            "falls_to_error": True,
            "reason": "no_relevant_documents",
        }
    scores = []
    for doc in docs:
        try:
            scores.append(int(str(llm.invoke(
                f"评估以下文档与问题的相关性(0-10):\n问题:{query}\n文档:{doc}"
            ).content).strip()))
        except (ValueError, AttributeError):
            # 仅把模型返回的非数字视为低分；调用超时、网络错误等异常必须交给错误处理层。
            scores.append(0)
    attempts = state.get("rag_attempts", 0)
    if max(scores) < 7 and attempts < state.get("rag_max_retries", 3):
        return {
            "rag_attempts": attempts + 1,
            "needs_self_correction": True,
            "correction_strategy": "rewrite_and_retry",
            "falls_to_error": False,
            "reason": "low_relevance",
        }
    if max(scores) < 7:
        return {
            "needs_self_correction": False,
            "correction_strategy": None,
            "falls_to_error": True,
            "reason": "relevance_retry_exhausted",
        }
    return {
        "needs_self_correction": False,
        "correction_strategy": None,
        "falls_to_error": False,
        "reason": None,
    }


def generate_rag_answer(state: AgentState) -> dict:
    """基于当前检索文档生成 RAG 草稿；不读取上一轮 final_response。"""
    docs = state.get("retrieved_docs") or []
    query = state.get("rewritten_query") or state["messages"][-1]["content"]
    prompt = f"""
    仅依据以下源文档回答问题；无法由文档支持的内容必须明确说明未知。
    问题：{query}
    源文档：{docs}
    """
    result = llm.invoke(prompt)
    # 每次生成都覆盖当前草稿，并清空上一轮检测结果，避免旧结果污染本轮判断。
    return {
        "rag_answer": result.content,
        "hallucination_check": None,
        "needs_self_correction": False,
        "correction_strategy": None,
        "falls_to_error": False,
        "reason": None,
    }


def check_hallucination(state: AgentState) -> dict:
    # 生产级 fail closed：不得回退到 final_response，它可能来自主图或旧 checkpoint。
    answer = state.get("rag_answer")
    docs = state.get("retrieved_docs") or []
    if not answer:
        return {
            "hallucination_check": {"faithful": False, "issues": ["missing_rag_answer"]},
            "falls_to_error": True,
            "needs_self_correction": False,
            "correction_strategy": None,
            "reason": "missing_rag_answer",
        }
    check_prompt = f"""
    检查以下答案是否基于提供的源文档，是否存在编造内容：
    答案：{answer}
    源文档：{docs}
    返回：{{ "faithful": true/false, "issues": [...] }}
    """
    try:
        result = llm.invoke(check_prompt)
        check = json.loads(result.content)
        if (
            not isinstance(check, dict)
            or type(check.get("faithful")) is not bool
            or not isinstance(check.get("issues"), list)
            or not all(isinstance(issue, str) for issue in check["issues"])
        ):
            raise ValueError("invalid_hallucination_check_schema")
    except (json.JSONDecodeError, TypeError, AttributeError, ValueError):
        return {
            "hallucination_check": {"faithful": False, "issues": ["invalid_hallucination_check"]},
            "falls_to_error": True,
            "needs_self_correction": False,
            "correction_strategy": None,
            "reason": "hallucination_check_invalid_output",
        }
    except Exception:
        # 调用端点不可用时同样 fail-closed；详细异常只进受控日志/trace。
        return {
            "hallucination_check": {"faithful": False, "issues": ["hallucination_check_unavailable"]},
            "falls_to_error": True,
            "needs_self_correction": False,
            "correction_strategy": None,
            "reason": "hallucination_check_failed",
        }
    attempts = state.get("hallucination_attempts", 0)
    if not check.get("faithful", False):
        if attempts < state.get("hallucination_max_retries", 2):
            return {
                "hallucination_check": check,
                "hallucination_attempts": attempts + 1,
                "needs_self_correction": True,
                "correction_strategy": "regenerate_from_sources",
                "falls_to_error": False,
                "reason": "unfaithful_rag_draft",
            }
        return {
            "hallucination_check": check,
            "falls_to_error": True,
            "needs_self_correction": False,
            "correction_strategy": None,
            "reason": "hallucination_retry_exhausted",
        }
    return {
        "hallucination_check": check,
        "needs_self_correction": False,
        "correction_strategy": None,
        "falls_to_error": False,
        "reason": None,
    }


def generate_response_node(state: AgentState) -> dict:
    """已验证的政策草稿直接提升为最终回复，避免检测后再次由 LLM 改写。"""
    check = state.get("hallucination_check")
    if (state.get("intent") == "policy"
            and state.get("rag_answer")
            and isinstance(check, dict)
            and check.get("faithful") is True
            and not state.get("falls_to_error")):
        return {"final_response": state["rag_answer"]}
    if state.get("intent") == "policy":
        return {
            "final_response": "暂时无法基于已验证的政策资料给出答复，已转人工处理。",
            "error": state.get("error") or state.get("reason") or "unverified_rag_answer",
        }
    return generate_non_rag_response(state)
```

> **路由边界**：`should_retry` 与 `hallucination_condition` 必须先判断 `falls_to_error`，再判断是否有可用重试预算。`falls_to_error=True` 从子图 `END` 返回后，由主图 `rag_result_condition` 进入 `handle_error`；不得在主图再次进入子图。`generate_response_node` 只能提升已经通过幻觉检测的 `rag_answer`，不得以新的 LLM 改写重新引入未检查的内容。

> **异常边界**：上例中的评分解析异常与 LLM 调用异常必须在实际代码中分开处理。调用超时、429、5xx 或连接失败不得静默转换为 `score=0`；应记录 `reason`，进入有界重试/熔断/人工升级路径。

## 5. Layer 4: 可观测性与评估层

### 5.1 全链路追踪（自托管 Langfuse）

```python
from langfuse import observe, get_client


@observe(name="after_sales_workflow")
async def run_workflow(user_input: str, ctx: TenantContext, thread_id: str):
    get_client().update_current_trace(
        name="after_sales_ticket",
        session_id=f"ticket_{thread_id}",
        user_id=ctx.user_id,
        metadata={"tenant_id": ctx.tenant_id, "workflow_type": "langgraph_crewai_hybrid", "version": "1.1.0"},
        tags=["ecommerce", "after-sales"],
    )
```

> 默认接入**自托管 Langfuse**，数据只在项目方控制的服务器、网络和备份边界内流转；可选 Prometheus/Grafana。若改用云端 LangSmith/Langfuse，需在部署备注中明确数据流、合规影响和“不再满足完全自托管”的变化。

### 5.2 核心评估指标

| 指标类别 | 具体指标 | 计算方式 |
| :-- | :-- | :-- |
| 业务指标 | 解决率 (Resolution Rate) | 成功处理的工单 / 总工单 |
| | 人工介入率 (Escalation Rate) | 转人工工单 / 总工单 |
| | 平均处理时长 (AHT) | 总处理时间 / 工单数 |
| 质量指标 | 忠实度 (Faithfulness) | 答案与源文档的一致程度 |
| | 答案相关性 (Answer Relevancy) | 答案与问题的相关程度 |
| | 完整性 (Completeness) | 答案覆盖问题的完整度 |
| 成本指标 | Token 消耗 | 每次请求的输入/输出 Token |
| | 单次成本 | Token 消耗 × 模型单价 |
| 性能指标 | 端到端延迟 | 从用户输入到最终回复的时间 |
| | 节点延迟 | 每个节点的执行时间 |

### 5.3 自动化评估

```python
import deepeval
from deepeval.metrics import FaithfulnessMetric, AnswerRelevancyMetric


def evaluate_response(question: str, answer: str, context: List[str]) -> dict:
    faithfulness = FaithfulnessMetric(threshold=0.7)
    relevancy = AnswerRelevancyMetric(threshold=0.7)
    faith_result = faithfulness.measure(answer=answer, context=context)
    rel_result = relevancy.measure(answer=answer, question=question)
    return {
        "faithfulness_score": faith_result.score,
        "relevancy_score": rel_result.score,
        "overall_pass": faith_result.passed and rel_result.passed,
    }
```

---

## 6. 与生产基线的分工

> 本文档为 **Agent 内部流程设计**；生产部署、前后端分离、范围分层、验收测试见 **《生产基线与验收测试》**，避免在多处重复维护同一选型表。
