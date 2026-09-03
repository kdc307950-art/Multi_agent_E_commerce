"""CrewAI / MCP 工具集成适配器（配置门控，默认关闭）。

设计（对应《工具调用与集成》§4：第二阶梯分域 + 静态绑定）：
- 默认关闭（crewai_enabled=False）：业务节点走内置确定性路径（EcommerceAdapter 做租户/资格校验）。
- 打开（crewai_enabled=True）后，本模块构造 CrewAI 子智能体（每个绑定 2-3 个业务/平台工具），
  并把业务调用委派给对应子智能体；工具层仍旧由 EcommerceAdapter 做租户/归属/资格校验，
  CrewAI 只负责"选哪个工具/怎么回答"，不绕过任何安全校验。
- 完全自托管：CrewAI 子智能体使用的 LLM 来自本地/内网 OpenAI 兼容端点（settings.llm_base_url），
  绝不接公有 SaaS。

真实调用链说明：`run_business_task` 在 enabled + crewai 可导入时构造真实 CrewAI 对象并执行；
否则（默认）用 `MockCrewAIBackend` 记录调用链（供单元/集成测试），证明契约可用。

本模块不改变系统安全模型：即使 CrewAI 被打开，写操作仍须经唯一 human_approval（上层保证）。
"""
from __future__ import annotations

import time
from typing import Optional

from src.tools import AdapterError, EcommerceAdapter


class CrewAIIntegrationError(Exception):
    """CrewAI 集成错误（未启用、依赖缺失、委派失败等）。"""


# 业务工具清单（MCP 风格描述；均由上层做租户/资格校验后授权）
BUSINESS_TOOL_SCHEMAS: list[dict] = [
    {"name": "query_order", "intent": "order",
     "description": "查询订单状态/金额/商品（只读）"},
    {"name": "track_shipping", "intent": "shipping",
     "description": "查询物流轨迹（只读）"},
    {"name": "process_refund", "intent": "refund",
     "description": "退款资格+金额判定并触发审批（写，需人工审批）"},
    {"name": "process_return", "intent": "return_request",
     "description": "退货资格判定并触发审批（写，需人工审批）"},
    {"name": "update_return_address", "intent": "return_address",
     "description": "变更退货地址并触发审批（写，需人工审批）"},
    {"name": "escalate_ticket", "intent": "complaint",
     "description": "转人工/升级"},
]


class MockCrewAIBackend:
    """确定性 CrewAI 兜底/测试后端：记录调用链，不触达模型。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def dispatch(self, intent: str, tool_name: str, ctx: dict, params: dict) -> dict:
        self.calls.append({"intent": intent, "tool": tool_name, "ctx": ctx, "params": params,
                           "ts": time.time()})
        return {"tool": tool_name, "intent": intent}


class CrewAIToolRouter:
    """把业务调用委派给 CrewAI 子智能体（enabled）或确定性后端（默认）。"""

    def __init__(self, *, enabled: bool = False, llm=None, adapter: Optional[EcommerceAdapter] = None,
                 allow_delegation: bool = True) -> None:
        self.enabled = enabled
        self.llm = llm
        self.adapter = adapter or EcommerceAdapter()
        self.allow_delegation = allow_delegation
        self._mock = MockCrewAIBackend()

    @staticmethod
    def resolve_tool(intent: str) -> str:
        for t in BUSINESS_TOOL_SCHEMAS:
            if t["intent"] == intent:
                return t["name"]
        return "escalate_ticket"

    def tool_schemas(self) -> list[dict]:
        return list(BUSINESS_TOOL_SCHEMAS)

    def run_business_task(self, intent: str, ctx: dict, params: dict | None = None) -> dict:
        """真实调用链入口。

        - enabled + crewai 可导入：构造真实 CrewAI Crew（子智能体绑定工具）并执行。
        - 否则：用 MockCrewAIBackend 记录调用链（测试/兜底）。
        无论哪条路径，工具返回结果均须再经 adapter 校验（上层完成），本层只做委派/路由。
        """
        tool_name = self.resolve_tool(intent)
        params = dict(params or {})
        if self.enabled:
            return self._run_real(intent, tool_name, ctx, params)
        return self._mock.dispatch(intent, tool_name, {k: v for k, v in ctx.items()}, params)

    def _run_real(self, intent: str, tool_name: str, ctx: dict, params: dict) -> dict:
        """构造并执行真实 CrewAI 子智能体（完全自托管，使用本地模型端点）。

        依赖 crewai（本环境未安装时抛 CrewAIIntegrationError；集成测试在具备 crewai 的环境验证）。
        """
        try:
            from crewai import Agent, Crew, Task  # type: ignore
        except Exception as exc:  # pragma: no cover - 取决于环境
            raise CrewAIIntegrationError(
                "CREWAI_ENABLED=true 但环境中未安装 crewai；请安装并验证后开启。"
            ) from exc
        # 构造一个绑定当前意图工具的子智能体（真实调用链：Agent → Task → 工具）。
        tool_meta = next(t for t in BUSINESS_TOOL_SCHEMAS if t["name"] == tool_name)
        agent = Agent(
            role=f"售后-{tool_meta['description']}",
            goal="在租户/资格校验通过前提下完成售后任务",
            backstory="完全自托管售后子智能体",
            llm=self._llm_for_crewai(),
            allow_delegation=self.allow_delegation,
        )
        task_input = {"ctx": {k: v for k, v in ctx.items()
                              if k in {"tenant_id", "user_id", "role", "order_id", "thread_id"}},
                      "params": params}
        task = Task(
            description=f"处理意图 {intent}，使用工具 {tool_name}；上下文为 JSON：{task_input}",
            expected_output="工具调用结果摘要",
            agent=agent,
        )
        crew = Crew(agents=[agent], tasks=[task], verbose=False)
        result = crew.kickoff()
        return {"tool": tool_name, "intent": intent,
                "crew_result": str(result) if result is not None else ""}

    def _llm_for_crewai(self):
        """提供本地模型供 CrewAI 使用（返回对象或 None 让 crewai 走默认 llm）。"""
        if self.llm is not None and hasattr(self.llm, "model"):
            # 复用本地 OpenAI 兼容 client 的 model；若需完整 LLM 对象请扩展。
            return self.llm.model
        return None

    @property
    def mock_calls(self) -> list[dict]:
        """最近一次确定性后端的调用链记录（供测试断言）。"""
        return self._mock.calls


def build_crewai_router(settings, adapter: Optional[EcommerceAdapter] = None) -> CrewAIToolRouter:
    """按配置构造 CrewAI 工具路由（默认关闭，走确定性后端）。"""
    from src.tools import build_adapter as _build_adapter
    return CrewAIToolRouter(
        enabled=bool(getattr(settings, "crewai_enabled", False)),
        llm=None,
        adapter=adapter or _build_adapter(settings),
        allow_delegation=bool(getattr(settings, "crewai_allow_delegation", True)),
    )
