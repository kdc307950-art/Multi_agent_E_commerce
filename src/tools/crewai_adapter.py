"""CrewAI / MCP 工具集成适配器（配置门控，默认关闭）。

设计（对应《工具调用与集成》§4：第二阶梯分域 + 静态绑定）：
- 默认关闭（crewai_enabled=False）：业务节点走内置确定性路径（EcommerceAdapter 做租户/资格校验）。
- 打开（crewai_enabled=True）后，本模块构造 CrewAI 子智能体，并把**真实工具对象**通过
  Agent 的 `tools` 参数绑定到子智能体（而非仅把工具名写进 Task 描述文本）。
- 工具层仍旧由 EcommerceAdapter 做租户/归属/资格校验；CrewAI 只负责"选哪个工具/怎么回答"，
  不绕过任何安全校验。
- 完全自托管：CrewAI 子智能体使用的 LLM 来自本地/内网 OpenAI 兼容端点（settings.llm_base_url），
  绝不接公有 SaaS（无本地端点/不在网络白名单时 fail-closed）。
- tenant_id / user_id / role 只在**服务端上下文**（调用方注入的 `ctx`）中，通过闭包注入到各个
  工具对象；模型只能提供业务参数（order_id / reason 等），**绝不能**传入或覆盖租户身份。

真实调用链说明：`run_business_task` 在 enabled + crewai 可导入时构造真实 CrewAI 对象并执行；
否则（默认）用 `MockCrewAIBackend` 记录调用链（供单元/集成测试），证明契约可用。

本模块不改变系统安全模型：即使 CrewAI 被打开，写操作（process_refund 等）只**创建待审批
操作**，绝不直接执行；唯一 human_approval 仍是写操作唯一入口（上层保证）。
"""
from __future__ import annotations

import json
import re
import time
from typing import Optional

import httpx

from src.core.types import OperationStatus, PendingAction, generate_operation_key
from src.tools import AdapterError, EcommerceAdapter


class CrewAIIntegrationError(Exception):
    """CrewAI 集成错误（未启用、依赖缺失、无自托管 LLM、委派失败等）。"""


# 业务工具清单（MCP 风格描述 + 输入/输出 schema；均由上层做租户/资格校验后授权）。
# 注意：schema 不暴露 tenant_id/user_id/role —— 这些身份字段由服务端上下文注入，绝不来自模型。
BUSINESS_TOOL_SCHEMAS: list[dict] = [
    {
        "name": "query_order",
        "intent": "order",
        "description": "查询订单状态/金额/商品（只读）",
        "input_schema": {
            "type": "object",
            "properties": {"order_id": {"type": "string", "description": "订单号 ORD-数字格式"}},
            "required": ["order_id"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string", "description": "订单状态/金额/商品摘要"}},
        },
    },
    {
        "name": "track_shipping",
        "intent": "shipping",
        "description": "查询物流轨迹（只读）",
        "input_schema": {
            "type": "object",
            "properties": {"order_id": {"type": "string", "description": "订单号 ORD-数字格式"}},
            "required": ["order_id"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string", "description": "当前物流状态/运单号"}},
        },
    },
    {
        "name": "process_refund",
        "intent": "refund",
        "description": "退款资格+金额判定并触发审批（写，只创建待审批操作，绝不执行）",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "订单号 ORD-数字格式"},
                "reason": {"type": "string", "description": "退款理由"},
            },
            "required": ["order_id"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["pending_approval"]},
                "operation_id": {"type": "string"},
                "approval_id": {"type": "string"},
            },
        },
    },
    {
        "name": "process_return",
        "intent": "return_request",
        "description": "退货资格判定并触发审批（写，只创建待审批操作，绝不执行）",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "订单号 ORD-数字格式"},
                "reason": {"type": "string", "description": "退货理由"},
            },
            "required": ["order_id"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["pending_approval"]},
                "operation_id": {"type": "string"},
                "approval_id": {"type": "string"},
            },
        },
    },
    {
        "name": "update_return_address",
        "intent": "return_address",
        "description": "变更退货地址并触发审批（写，只创建待审批操作，绝不执行）",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "订单号 ORD-数字格式"},
                "receiver_name": {"type": "string"},
                "phone": {"type": "string"},
                "region": {"type": "string"},
                "detail": {"type": "string"},
            },
            "required": ["order_id"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["pending_approval"]},
                "operation_id": {"type": "string"},
                "approval_id": {"type": "string"},
            },
        },
    },
    {
        "name": "escalate_ticket",
        "intent": "complaint",
        "description": "转人工/升级",
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string", "description": "升级/转人工理由"}},
            "required": [],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string", "description": "已转人工"}},
        },
    },
]


# 每个意图绑定的工具子集（第二阶梯：子智能体分域 + 静态绑定；每个子智能体只绑 2-3 个）。
_INTENT_TOOLS: dict[str, list[str]] = {
    "order": ["query_order"],
    "shipping": ["query_order"],
    "refund": ["process_refund", "query_order"],
    "return_request": ["process_return", "query_order"],
    "return_address": ["update_return_address", "query_order"],
    "complaint": ["escalate_ticket"],
}


def _require_ctx(ctx: dict, key: str) -> str:
    """服务端上下文必填字段；缺失即 fail-closed（不能以默认租户/身份继续）。"""
    value = ctx.get(key)
    if value is None or value == "":
        raise AdapterError("missing_tenant_context", f"缺少服务端上下文 {key}，拒绝执行")
    return value


def _validated_order_id(ctx: dict, params: dict) -> str:
    """只接受服务端已确认的订单号，拒绝模型改写或注入其他定位值。"""
    context_order_id = ctx.get("order_id")
    supplied_order_id = params.get("order_id")
    if supplied_order_id is not None:
        if not isinstance(supplied_order_id, str) or not re.fullmatch(r"ORD-\d+", supplied_order_id):
            raise AdapterError("invalid_order_id", "订单号格式非法，转人工")
        if context_order_id and supplied_order_id != context_order_id:
            raise AdapterError("order_id_mismatch", "模型提供的订单号与服务端上下文不一致，转人工")
    order_id = supplied_order_id or context_order_id
    if not order_id:
        raise AdapterError("missing_order_id", "缺少订单号")
    if not isinstance(order_id, str) or not re.fullmatch(r"ORD-\d+", order_id):
        raise AdapterError("invalid_order_id", "订单号格式非法，转人工")
    return order_id


class MockCrewAIBackend:
    """确定性 CrewAI 兜底/测试后端：记录调用链，不触达模型。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def dispatch(self, intent: str, tool_name: str, ctx: dict, params: dict) -> dict:
        self.calls.append({"intent": intent, "tool": tool_name, "ctx": ctx, "params": params,
                           "ts": time.time()})
        return {"tool": tool_name, "intent": intent}


class CrewAIToolRouter:
    """把业务调用委派给 CrewAI 子智能体（enabled）或确定性后端（默认）。

    安全要点：
    - `store`（可选）：写工具创建待审批 operation/approval 必需；缺失时写调用 fail-closed 转人工。
    - `settings`（可选）：提供自托管 LLM 端点（base_url/model/api_key）与网络白名单；
      缺失或端点不在白名单时 `_run_real` fail-closed，绝不回退到公有 SaaS 默认。
    - `ctx` 由调用方注入（服务端 TenantContext），工具对象通过闭包捕获它；模型不可覆盖租户身份。
    """

    def __init__(self, *, enabled: bool = False, llm=None, adapter: Optional[EcommerceAdapter] = None,
                 store=None, settings=None, allow_delegation: bool = True) -> None:
        self.enabled = enabled
        self.llm = llm
        self.adapter = adapter or EcommerceAdapter()
        self.store = store
        self.settings = settings
        self.allow_delegation = allow_delegation
        self._mock = MockCrewAIBackend()
        # 最近一次写工具返回的业务 dict（operation_id/approval_id/refund_amount/status）。
        # 供 `_run_real` 把写元数据上抛到返回顶层，使主图能进入唯一 human_approval；
        # 每次 run_business_task 起始重置，避免跨调用残留（路由在 builder 中按次构造）。
        self._last_write_meta: Optional[dict] = None

    @staticmethod
    def resolve_tool(intent: str) -> str:
        # Accept both graph intents ("order", "refund", ...) and the public
        # tool names used by direct CrewAI integration callers/tests.
        if intent in {t["name"] for t in BUSINESS_TOOL_SCHEMAS}:
            return intent
        for t in BUSINESS_TOOL_SCHEMAS:
            if t["intent"] == intent:
                return t["name"]
        return "escalate_ticket"

    def tool_schemas(self) -> list[dict]:
        return list(BUSINESS_TOOL_SCHEMAS)

    # -------------------------------------------------------------------------
    # 写操作能力门控（模型白名单，与主图节点一致：写风险工具只允许白名单模型）。
    # -------------------------------------------------------------------------
    def _write_capability_ok(self) -> bool:
        if self.llm is not None:
            return bool(getattr(self.llm, "capability_ok", False))
        if self.settings is not None:
            from src.llm.capability import capability_ok
            model = getattr(self.settings, "effective_llm_model", "") or ""
            return capability_ok(model, self.settings)
        # 无任何上下文 → 保守拒绝写（fail-closed）。
        return False

    # -------------------------------------------------------------------------
    # 业务工具实现（可独立于 crewai 单测：纯业务逻辑，全部由服务端 ctx 注入身份）。
    # -------------------------------------------------------------------------
    def _do_query_order(self, ctx: dict, params: dict) -> str:
        tenant_id = _require_ctx(ctx, "tenant_id")
        user_id = _require_ctx(ctx, "user_id")
        role = ctx.get("role") or "customer"
        order_id = _validated_order_id(ctx, params)
        order = self.adapter.get_order(tenant_id, user_id, role, order_id)
        items = ", ".join(f'{i["name"]}(x{i["quantity"]})' for i in order.items)
        return f'状态:{order.status} 金额:{order.total_amount} 商品:{items}'

    def _do_process_refund(self, ctx: dict, params: dict) -> dict:
        """退款：只做资格判定并创建待审批 operation/approval，**绝不执行**。"""
        if not self._write_capability_ok():
            raise AdapterError("model_not_in_whitelist", "写操作模型不在能力矩阵白名单，转人工")
        tenant_id = _require_ctx(ctx, "tenant_id")
        user_id = _require_ctx(ctx, "user_id")
        role = ctx.get("role") or "customer"
        order_id = _validated_order_id(ctx, params)
        if self.store is None:
            # 无存储无法创建审批操作 → fail-closed 转人工。
            raise AdapterError("missing_store", "无存储，无法创建审批操作；转人工")
        thread_id = ctx.get("thread_id") or ""
        now = time.time()
        # 资格判定（归属/金额/窗口），不通过则 fail-closed 转人工。
        elig = self.adapter.check_refund_eligibility(tenant_id, user_id, role, order_id)
        if not elig.eligible:
            raise AdapterError("refund_ineligible", elig.detail or "退款资格/金额不明，转人工")
        reason = params.get("reason") or "申请退款"
        client_request_id = (params.get("client_request_id") or ctx.get("client_request_id")
                             or str(int(now * 1000)))
        idem_key = generate_operation_key(PendingAction.REFUND, tenant_id, order_id, client_request_id)
        op = self.store.create_operation(tenant_id, thread_id, order_id, PendingAction.REFUND,
                                         idem_key, now)
        approval = self._get_or_create_approval(
            tenant_id, thread_id, op.operation_id, PendingAction.REFUND,
            order_id, elig.amount, reason, now)
        # 只创建待审批操作；审批通过后由 execute_* 执行（本模块绝不执行）。
        return {
            "status": "pending_approval",
            "operation_id": op.operation_id,
            "approval_id": approval.approval_id,
            "refund_amount": elig.amount,
            "message": f"退款申请已创建（金额 {elig.amount}），需人工审批后执行。",
        }

    def _do_process_return(self, ctx: dict, params: dict) -> dict:
        """退货：资格判定后只创建待审批 operation/approval。"""
        if not self._write_capability_ok():
            raise AdapterError("model_not_in_whitelist", "写操作模型不在能力矩阵白名单，转人工")
        tenant_id = _require_ctx(ctx, "tenant_id")
        user_id = _require_ctx(ctx, "user_id")
        role = ctx.get("role") or "customer"
        order_id = _validated_order_id(ctx, params)
        if self.store is None:
            raise AdapterError("missing_store", "无存储，无法创建审批操作；转人工")
        elig = self.adapter.check_return_eligibility(tenant_id, user_id, role, order_id)
        if not elig.eligible:
            raise AdapterError("return_ineligible", elig.detail or "退货资格不明，转人工")
        now = time.time()
        client_request_id = (params.get("client_request_id") or ctx.get("client_request_id")
                             or str(int(now * 1000)))
        idem_key = generate_operation_key(PendingAction.RETURN_REQUEST, tenant_id, order_id,
                                           client_request_id)
        thread_id = ctx.get("thread_id") or ""
        op = self.store.create_operation(tenant_id, thread_id, order_id,
                                         PendingAction.RETURN_REQUEST, idem_key, now)
        reason = params.get("reason") or "申请退货"
        approval = self._get_or_create_approval(
            tenant_id, thread_id, op.operation_id, PendingAction.RETURN_REQUEST,
            order_id, None, reason, now)
        result = {"status": "pending_approval", "operation_id": op.operation_id,
                  "approval_id": approval.approval_id, "message": "退货申请已创建，需人工审批后执行。"}
        return result

    def _do_update_return_address(self, ctx: dict, params: dict) -> dict:
        """改址：校验地址后只创建待审批 operation/approval。"""
        if not self._write_capability_ok():
            raise AdapterError("model_not_in_whitelist", "写操作模型不在能力矩阵白名单，转人工")
        tenant_id = _require_ctx(ctx, "tenant_id")
        user_id = _require_ctx(ctx, "user_id")
        role = ctx.get("role") or "customer"
        order_id = _validated_order_id(ctx, params)
        if self.store is None:
            raise AdapterError("missing_store", "无存储，无法创建审批操作；转人工")
        address_keys = ("receiver_name", "phone", "region", "detail")
        address = {k: params.get(k) for k in address_keys if params.get(k) is not None}
        normalized = self.adapter.validate_address_change(tenant_id, user_id, role, order_id, address)
        now = time.time()
        client_request_id = (params.get("client_request_id") or ctx.get("client_request_id")
                             or str(int(now * 1000)))
        idem_key = generate_operation_key(PendingAction.RETURN_ADDRESS, tenant_id, order_id,
                                           client_request_id)
        thread_id = ctx.get("thread_id") or ""
        op = self.store.create_operation(tenant_id, thread_id, order_id,
                                         PendingAction.RETURN_ADDRESS, idem_key, now)
        reason = f"变更退货地址至 {normalized['region']} {normalized['detail']}"
        approval = self._get_or_create_approval(
            tenant_id, thread_id, op.operation_id, PendingAction.RETURN_ADDRESS,
            order_id, None, reason, now)
        return {"status": "pending_approval", "operation_id": op.operation_id,
                "approval_id": approval.approval_id, "message": "退货地址变更已创建，需人工审批后执行。"}

    def _get_or_create_approval(self, tenant_id: str, thread_id: str, operation_id: str,
                                action: PendingAction, order_id: str, amount: Optional[float],
                                reason: str, now: float):
        """审批幂等：同一 operation 只允许一张审批单，兼容各存储后端。"""
        for approval in self.store.list_approvals(tenant_id):
            if approval.operation_id == operation_id:
                return approval
        return self.store.create_approval(tenant_id, thread_id, operation_id, action,
                                           order_id, amount, reason, now)

    @staticmethod
    def _do_escalate_ticket(ctx: dict, params: dict) -> str:
        return "已转为人工处理"

    # -------------------------------------------------------------------------
    # CrewAI 工具工厂：把业务逻辑包装成 crewai Tool 对象，并通过闭包注入服务端 ctx。
    # 工具函数只暴露模型可控参数（order_id / reason 等），**不暴露** tenant/user/role。
    # -------------------------------------------------------------------------
    def _make_query_order_tool(self, crewai_tool, ctx: dict):
        def query_order(order_id: str) -> str:
            """查询订单状态/金额/商品（只读）。参数：order_id（ORD-数字格式）。"""
            return self._do_query_order(ctx, {"order_id": order_id})

        return crewai_tool(query_order)

    def _make_process_refund_tool(self, crewai_tool, ctx: dict):
        def process_refund(order_id: str, reason: str = "") -> str:
            """退款资格判定并触发人工审批（写操作，只创建待审批记录，绝不直接退款）。
            参数：order_id（ORD-数字格式）、reason（退款理由）。"""
            result = self._do_process_refund(ctx, {"order_id": order_id, "reason": reason})
            # 把写工具的业务 dict 透传给 `_run_real`（顶层上抛 approval_id/operation_id），
            # 使主图 refund 分支能识别；同时返回给 crew 的仍是 JSON 字符串（工具契约）。
            self._last_write_meta = dict(result)
            return json.dumps(result, ensure_ascii=False)

        return crewai_tool(process_refund)

    def _make_process_return_tool(self, crewai_tool, ctx: dict):
        def process_return(order_id: str, reason: str = "") -> str:
            result = self._do_process_return(ctx, {"order_id": order_id, "reason": reason})
            self._last_write_meta = dict(result)
            return json.dumps(result, ensure_ascii=False)
        return crewai_tool(process_return)

    def _make_update_return_address_tool(self, crewai_tool, ctx: dict):
        def update_return_address(order_id: str, receiver_name: str = "", phone: str = "",
                                  region: str = "", detail: str = "") -> str:
            result = self._do_update_return_address(ctx, {
                "order_id": order_id, "receiver_name": receiver_name, "phone": phone,
                "region": region, "detail": detail,
            })
            self._last_write_meta = dict(result)
            return json.dumps(result, ensure_ascii=False)
        return crewai_tool(update_return_address)

    def _make_escalate_ticket_tool(self, crewai_tool, ctx: dict):
        def escalate_ticket(reason: str = "") -> str:
            """转人工/升级（无法自动处理或客户升级投诉时）。参数：reason（升级/转人工理由）。"""
            return self._do_escalate_ticket(ctx, {"reason": reason})

        return crewai_tool(escalate_ticket)

    def _build_bound_tools(self, crewai_tool, intent: str, ctx: dict) -> list:
        """按意图静态绑定一个小工具集（分域）；未定义写工具的意图 fail-closed 绑定转人工。"""
        # Public callers may pass either a graph intent (``order``) or the
        # resolved tool name (``query_order``).  Normalize both forms before
        # selecting the static domain, otherwise a direct ``query_order``
        # invocation would silently receive only the escalation tool.
        canonical_intent = next(
            (item["intent"] for item in BUSINESS_TOOL_SCHEMAS if item["name"] == intent),
            intent,
        )
        names = _INTENT_TOOLS.get(canonical_intent, ["escalate_ticket"])
        tools: list = []
        for name in names:
            if name == "query_order":
                tools.append(self._make_query_order_tool(crewai_tool, ctx))
            elif name == "process_refund":
                tools.append(self._make_process_refund_tool(crewai_tool, ctx))
            elif name == "process_return":
                tools.append(self._make_process_return_tool(crewai_tool, ctx))
            elif name == "update_return_address":
                tools.append(self._make_update_return_address_tool(crewai_tool, ctx))
            elif name == "escalate_ticket":
                tools.append(self._make_escalate_ticket_tool(crewai_tool, ctx))
            else:
                # 未实现绑定的工具：一律 fail-closed 只允许转人工。
                tools.append(self._make_escalate_ticket_tool(crewai_tool, ctx))
        return tools

    # -------------------------------------------------------------------------
    # 自托管 LLM：仅使用本地/内网端点；无端点或不在网络白名单 → fail-closed，绝不接 SaaS。
    # -------------------------------------------------------------------------
    def _crewai_llm(self):
        from crewai import LLM

        # LiteLLM cannot infer function-calling support for an arbitrary
        # self-hosted model id.  CrewAI otherwise falls back to ReAct text
        # parsing and may never submit the bound tools, even when the endpoint
        # returns a valid OpenAI ``tool_calls`` response.  Keep the override
        # local to the self-hosted adapter so this claim is explicit and
        # auditable; the endpoint still must return/parse standard tool calls.
        class _SelfHostedLLM(LLM):
            """给 CrewAI 默认 ReAct 执行器补一个显式原生工具调用入口。

            CrewAI 0.152 的普通 Agent 首轮调用不会把 tools 传给主 LLM，
            小型自托管模型因此会退化成文本格式猜测。这里仅在本适配器配置
            了静态工具集时，先发起一次带 tools/tool_choice 的原生调用；工具
            没有实际执行则 fail-closed，不接受模型生成的伪摘要。
            """

            def configure_native_tool_calling(self, tools, functions, tool_name):
                self._native_tools = list(tools or [])
                self._native_functions = dict(functions or {})
                self._native_tool_name = str(tool_name or "")
                self._native_attempted = False
                self._native_executed = False
                # Unit tests use lightweight CrewAI stand-ins that do not
                # implement the real LLM.call contract. Keep those on the
                # existing fake Crew path; enable direct mode only when the
                # actual CrewAI base class exposes call().
                self._native_direct_mode = callable(getattr(LLM, "call", None))

            def call(self, messages, tools=None, callbacks=None,
                     available_functions=None, from_task=None, from_agent=None):
                native_tools = getattr(self, "_native_tools", [])
                if native_tools and tools and not getattr(self, "_native_attempted", False):
                    # 显式把工具 schema 传给 CrewAI，避免其默认 ReAct 路径自行猜格式。
                    self._native_attempted = True
                    previous_choice = self.additional_params.pop("tool_choice", None)
                    self.additional_params["tool_choice"] = {
                        "type": "function",
                        "function": {"name": self._native_tool_name},
                    }
                    try:
                        try:
                            result = super().call(
                                messages, tools=tools, callbacks=callbacks,
                                available_functions=available_functions,
                                from_task=from_task, from_agent=from_agent,
                            )
                        except Exception:
                            result = self._direct_native_call(messages)
                        if not getattr(self, "_native_executed", False):
                            result = self._direct_native_call(messages)
                        return result
                    finally:
                        self.additional_params.pop("tool_choice", None)
                        if previous_choice is not None:
                            self.additional_params["tool_choice"] = previous_choice
                if (native_tools and getattr(self, "_native_attempted", False)
                        and not getattr(self, "_native_executed", False) and not tools):
                    # CrewAI may retry after a malformed/text response. Do not let
                    # those retries re-enter its ReAct parser and invoke tools with
                    # guessed arguments; the native attempt is already terminal.
                    raise CrewAIIntegrationError(
                        "自托管 LLM 原生工具调用失败，拒绝 ReAct 文本重试。"
                    )
                if native_tools and not tools and not getattr(self, "_native_attempted", False):
                    self._native_attempted = True
                    # 只在这次带工具的请求中注入 tool_choice，避免污染后续普通调用。
                    previous_choice = self.additional_params.pop("tool_choice", None)
                    self.additional_params["tool_choice"] = {
                        "type": "function",
                        "function": {"name": self._native_tool_name},
                    }
                    try:
                        try:
                            result = super().call(
                                messages,
                                tools=native_tools,
                                callbacks=callbacks,
                                available_functions=self._native_functions,
                                from_task=from_task,
                                from_agent=from_agent,
                            )
                        except Exception:
                            # A transient local endpoint error (503/timeout) must
                            # not enter CrewAI's ReAct retry loop. Try the compact
                            # native request once; any failure remains fail-closed.
                            result = self._direct_native_call(messages)
                    finally:
                        self.additional_params.pop("tool_choice", None)
                        if previous_choice is not None:
                            self.additional_params["tool_choice"] = previous_choice
                    if not getattr(self, "_native_executed", False):
                        # LiteLLM 可能已成功返回 HTTP 200，但把模型普通文本直接
                        # 当成 LLM 结果。用简洁原生请求再试一次，仍失败则 fail-closed。
                        result = self._direct_native_call(messages)
                    # 已经在 LLM 层执行过绑定工具，返回最终答案以结束 CrewAI ReAct 循环，
                    # 避免执行器再次重复调用同一工具。
                    return f"Thought: 原生工具调用已完成。\nFinal Answer: {result}"
                return super().call(
                    messages,
                    tools=tools,
                    callbacks=callbacks,
                    available_functions=available_functions,
                    from_task=from_task,
                    from_agent=from_agent,
                )

            def mark_native_tool_executed(self):
                self._native_executed = True

            def _direct_native_call(self, messages):
                """重试本地兼容端点并严格执行唯一绑定工具。"""
                endpoint = str(getattr(self, "base_url", "") or getattr(self, "api_base", ""))
                if not endpoint:
                    raise CrewAIIntegrationError("自托管 LLM 缺少 base_url，拒绝降级为文本结果。")
                endpoint = endpoint.rstrip("/")
                if not endpoint.endswith("/chat/completions"):
                    endpoint = f"{endpoint}/chat/completions"
                model = str(getattr(self, "model", "") or "")
                if model.startswith("openai/"):
                    model = model.split("/", 1)[1]
                text = ""
                if isinstance(messages, list):
                    # CrewAI 0.152 may collapse the task into a system message;
                    # inspect all message content for the already validated order
                    # locator, while never extracting tenant/user/role identity.
                    text = "\n".join(
                        str(item.get("content") or "")
                        for item in messages
                        if isinstance(item, dict)
                    )
                if not text:
                    text = "请调用已绑定工具完成任务。"
                order_match = re.search(r"ORD-\d+", text)
                # 小型模型在 CrewAI 长系统提示下容易退化为普通文本；原生重试
                # 使用最小、确定的业务指令，避免把 ReAct 格式要求带回模型。
                order_id = order_match.group(0) if order_match else ""
                if self._native_tool_name == "query_order" and order_id:
                    compact = f"查询订单 {order_id}，只调用 query_order 工具。"
                elif self._native_tool_name == "process_refund" and order_id:
                    compact = f"申请订单 {order_id} 退款，只调用 process_refund 工具。"
                else:
                    compact = f"{text}\n只调用 {self._native_tool_name} 工具。"
                payload = {
                    "model": model,
                    "messages": [{"role": "user", "content": compact}],
                    "tools": self._native_tools,
                    "tool_choice": {"type": "function", "function": {"name": self._native_tool_name}},
                    "temperature": 0,
                    "max_tokens": self.max_tokens or self.max_completion_tokens or 512,
                }
                if isinstance(self.additional_params, dict) and "think" in self.additional_params:
                    payload["think"] = self.additional_params["think"]
                headers = {"Authorization": f"Bearer {self.api_key}"} if getattr(self, "api_key", None) else {}
                try:
                    response = httpx.post(endpoint, json=payload, headers=headers,
                                          timeout=float(getattr(self, "timeout", 10.0)))
                    response.raise_for_status()
                    message = response.json().get("choices", [{}])[0].get("message", {})
                    calls = message.get("tool_calls") or []
                    if len(calls) != 1:
                        raise CrewAIIntegrationError(
                            "自托管端点必须返回恰好一个原生 tool_call，拒绝歧义结果。"
                        )
                    function = calls[0].get("function", {})
                    name = function.get("name")
                    if name != self._native_tool_name or name not in self._native_functions:
                        raise CrewAIIntegrationError(f"端点返回未授权工具 {name!r}，拒绝执行。")
                    arguments = json.loads(function.get("arguments") or "{}")
                    if not isinstance(arguments, dict):
                        raise CrewAIIntegrationError("端点返回的工具参数不是 JSON 对象。")
                    result = self._native_functions[name](**arguments)
                    self.mark_native_tool_executed()
                    return result
                except CrewAIIntegrationError:
                    raise
                except Exception as exc:
                    raise CrewAIIntegrationError(f"自托管原生工具调用失败：{exc}") from exc

            def supports_function_calling(self) -> bool:  # pragma: no cover - exercised by CrewAI
                return True

        # 调用方已显式提供 crewai 兼容 LLM 实例（有 model + base_url），优先使用。
        if self.llm is not None and hasattr(self.llm, "model") and (
                hasattr(self.llm, "base_url") or hasattr(self.llm, "api_base")):
            return self.llm

        s = self.settings
        if s is None:
            raise CrewAIIntegrationError(
                "crewai_enabled=true 但未提供自托管 LLM 配置；拒绝使用公有 SaaS 默认端点。")
        base_url = str(getattr(s, "llm_base_url", "") or "")
        model = str(getattr(s, "effective_llm_model", "") or getattr(s, "llm_model", "self-hosted-model"))
        api_key = str(getattr(s, "llm_api_key", "sk-local"))
        if not base_url:
            raise CrewAIIntegrationError(
                "crewai_enabled=true 但未配置自托管 LLM 端点；拒绝使用公有 SaaS 默认端点。")
        # 网络白名单守卫（完全自托管红线）：端点不在白名单一律拒绝。
        from src.llm.security import EndpointGuard
        guard = EndpointGuard(list(getattr(s, "llm_allowed_host_list", []) or []),
                              restricted=bool(getattr(s, "is_restricted_env", False)))
        if not guard.allowed(base_url):
            raise CrewAIIntegrationError(
                f"CrewAI 端点 {base_url} 不在自托管网络白名单内，拒绝访问。")
        # CrewAI 通过 LiteLLM 路由模型；裸的自托管模型名无法推断 provider，
        # 会在真实调用前被拒绝。显式使用 openai/ 前缀仍指向项目自托管的
        # OpenAI-compatible base_url，不会切换到公有 OpenAI 服务。
        if "/" not in model:
            model = f"openai/{model}"
        llm_kwargs = {
            "model": model,
            "base_url": base_url,
            "api_key": api_key,
            "timeout": float(getattr(s, "llm_timeout_seconds", 10.0)),
            "max_tokens": int(getattr(s, "llm_max_tokens", 512)),
        }
        if bool(getattr(s, "llm_disable_thinking", False)):
            # Ollama 的 OpenAI-compatible API 接受该字段以关闭 Qwen3 的思考输出；
            # 不支持它的 LiteLLM provider 会在本地参数归一化时忽略。
            llm_kwargs["think"] = False
        return _SelfHostedLLM(**llm_kwargs)

    @staticmethod
    def _task_description(intent: str, tool_name: str, params: dict) -> str:
        """构造最小任务描述，不把服务端身份上下文暴露给模型。

        ``order_id`` 是已由上游解析并校验过的业务定位字段，不是身份字段。真实
        工具调用需要这个值才能避免模型猜测订单号；其余业务参数和全部
        ``tenant_id`` / ``user_id`` / ``role`` 均不进入提示词。
        """
        tool_desc = next((t["description"] for t in BUSINESS_TOOL_SCHEMAS if t["name"] == tool_name),
                         tool_name)
        description = (
            f"请处理售后意图「{intent}」。使用已绑定工具「{tool_name}」完成：{tool_desc}。"
        )
        # 只接受预期的订单号格式，避免将任意用户文本回灌到工具选择提示词。
        order_id = params.get("order_id")
        if isinstance(order_id, str) and re.fullmatch(r"ORD-\d+", order_id):
            description += f"服务端已确认本次订单号为「{order_id}」，调用工具时必须使用该订单号。"
        description += "只调用已授权工具，不得编造订单或金额数据；若无法确定则转人工。"
        return description

    def run_business_task(self, intent: str, ctx: dict, params: dict | None = None) -> dict:
        """真实调用链入口。

        - enabled + crewai 可导入：构造真实 CrewAI Crew（子智能体绑定真实工具对象）并执行。
        - 否则：用 MockCrewAIBackend 记录调用链（测试/兜底）。
        无论哪条路径，工具返回结果均须再经 adapter 校验（上层完成），本层只做委派/路由。

        孤儿清理：真实链路失败（委派异常/写工具已建 pending 后失败）时，若已创建待审批
        operation/approval，会回滚为「拒绝/转人工」并审计留痕，避免留下无审批关联的孤儿记录。
        """
        tool_name = self.resolve_tool(intent)
        params = dict(params or {})
        if not self.enabled:
            return self._mock.dispatch(intent, tool_name, {k: v for k, v in ctx.items()}, params)
        # 起始重置：避免上一次调用的写元数据残留（路由在 builder 中按次构造，此处双保险）。
        self._last_write_meta = None
        try:
            return self._run_real(intent, tool_name, ctx, params)
        except Exception as exc:
            # 委派失败：若写工具已创建待审批 operation/approval（孤儿），回滚/补偿并留痕。
            self._rollback_orphan_write(ctx, exc)
            raise

    def _extract_write_meta(self, crew_result: str) -> dict:
        """从 crew 工具输出字符串中恢复业务 dict（写工具返回值是 json 字符串）。

        作为 `_last_write_meta` 的兜底：真实 crewai 环境下 kickoff 输出格式不一，若工具闭包
        未直接回传 meta，仍从输出字符串解析出 operation_id/approval_id/refund_amount/status。
        """
        if not crew_result:
            return {}
        # 逐段尝试从字符串中提取 JSON 对象（容忍文本包裹/换行）。
        import re
        meta: dict = {}
        for m in re.finditer(r"\{[^{}]*\}", crew_result):
            chunk = m.group(0)
            try:
                data = json.loads(chunk)
            except (ValueError, TypeError):
                continue
            if not isinstance(data, dict):
                continue
            if data.get("operation_id") or data.get("approval_id"):
                for k in ("status", "operation_id", "approval_id", "refund_amount", "message"):
                    if data.get(k) is not None:
                        meta[k] = data[k]
                break
        return meta

    def _run_real(self, intent: str, tool_name: str, ctx: dict, params: dict) -> dict:
        """构造并执行真实 CrewAI 子智能体，把**真实工具对象**绑定到 Agent（非仅描述文本）。

        依赖 crewai（本环境未安装时抛 CrewAIIntegrationError；集成测试在具备 crewai 的环境验证）。
        返回 dict 一定带 `tool/intent/crew_result`；写路径还会把 `approval_id/operation_id/
        refund_amount/status` 上抛到顶层（与 `_do_process_refund` 形状对齐），使主图 refund 分支
        能进入唯一 human_approval。
        """
        try:
            from crewai import Agent, Crew, Task  # type: ignore
            from crewai.tools import tool as crewai_tool  # type: ignore
        except Exception as exc:  # pragma: no cover - 取决于环境
            raise CrewAIIntegrationError(
                "CREWAI_ENABLED=true 但环境中未安装 crewai；请安装并验证后开启。"
            ) from exc
        # 构造绑定当前意图工具集的子智能体（真实调用链：Agent → tools → 工具 → adapter 校验）。
        tools = self._build_bound_tools(crewai_tool, intent, dict(ctx))
        crew_llm = self._crewai_llm()
        if hasattr(crew_llm, "configure_native_tool_calling"):
            # 将 CrewAI 工具对象转换成 OpenAI-compatible schema；函数闭包仍保留
            # 服务端 TenantContext，模型只能提供业务参数。
            tool_names = [getattr(tool, "name", "") for tool in tools]
            native_tools = []
            for name in tool_names:
                spec = next((item for item in BUSINESS_TOOL_SCHEMAS if item["name"] == name), None)
                if spec:
                    native_tools.append({
                        "type": "function",
                        "function": {
                            "name": spec["name"],
                            "description": spec["description"],
                            "parameters": spec["input_schema"],
                        },
                    })

            native_functions = {}
            for tool in tools:
                name = getattr(tool, "name", "")
                if not name:
                    continue
                def _invoke(_tool=tool, **kwargs):
                    result = _tool.run(**kwargs)
                    if hasattr(crew_llm, "mark_native_tool_executed"):
                        crew_llm.mark_native_tool_executed()
                    return result
                native_functions[name] = _invoke
            crew_llm.configure_native_tool_calling(native_tools, native_functions, tool_name)
            # 对自托管端点走一次短提示的原生 CrewAI LLM 调用，避免 CrewAI
            # ReAct 执行器把长系统提示交给小模型后退化为文本 Action。
            # 工具仍是 CrewAI Tool 对象，函数闭包仍执行完整租户/权限校验。
            if getattr(crew_llm, "_native_direct_mode", False):
                direct_prompt = self._task_description(intent, tool_name, params)
                direct_result = crew_llm.call(
                    [{"role": "user", "content": direct_prompt}],
                    tools=native_tools,
                    available_functions=native_functions,
                )
                if not getattr(crew_llm, "_native_executed", False):
                    raise CrewAIIntegrationError(
                        "自托管 LLM 原生 tool_call 未执行绑定工具，拒绝接受文本结果。"
                    )
                crew_result = str(direct_result) if direct_result is not None else ""
                out = {"tool": tool_name, "intent": intent, "crew_result": crew_result}
                meta = self._last_write_meta if isinstance(self._last_write_meta, dict) else {}
                for key in ("status", "operation_id", "approval_id", "refund_amount"):
                    if meta.get(key) is not None:
                        out[key] = meta[key]
                if meta.get("message"):
                    out["approval_reason"] = meta["message"]
                return out
        agent = Agent(
            role=f"售后-{tool_name}",
            goal="只在租户/资格校验通过前提下完成售后任务；写操作只提交审批，不直接执行",
            backstory="完全自托管售后子智能体，只调用已授权工具",
            llm=crew_llm,
            # Bound the local demo loop: a small self-hosted model must fail
            # closed quickly instead of spinning through unbounded ReAct turns.
            max_iter=3,
            max_execution_time=30,
            function_calling_llm=crew_llm,
            allow_delegation=self.allow_delegation,
            tools=tools,
        )
        task = Task(
            description=self._task_description(intent, tool_name, params),
            expected_output="工具调用结果摘要",
            agent=agent,
        )
        crew = Crew(agents=[agent], tasks=[task], verbose=False)
        result = crew.kickoff()
        crew_result = str(result) if result is not None else ""
        out = {"tool": tool_name, "intent": intent, "crew_result": crew_result}
        # 上抛写元数据到顶层（与 `_do_process_refund` 形状对齐），使主图 refund 分支进入 human_approval。
        meta = self._last_write_meta if isinstance(self._last_write_meta, dict) else {}
        if not meta:
            meta = self._extract_write_meta(crew_result)
        if meta:
            for k in ("status", "operation_id", "approval_id", "refund_amount"):
                if meta.get(k) is not None:
                    out[k] = meta[k]
            if meta.get("message"):
                out["approval_reason"] = meta["message"]
        return out

    def _rollback_orphan_write(self, ctx: dict, exc: Exception) -> None:
        """回滚/补偿已创建待审批但最终委派失败的孤儿 operation/approval。

        仅做「拒绝 + 转人工」的收口（绝不执行）；若写工具在建 operation 之前就失败（如模型不在
        白名单、资格不符、缺租户上下文），则不产生任何记录，无需回滚。审计留痕以便追溯。
        """
        meta = self._last_write_meta if isinstance(self._last_write_meta, dict) else {}
        operation_id = meta.get("operation_id")
        approval_id = meta.get("approval_id")
        tenant_id = ctx.get("tenant_id")
        if not tenant_id or not operation_id:
            return
        if self.store is None:
            return
        reason = f"CrewAI 委派失败，转人工对账（孤儿清理）：{exc}"
        try:
            self.store.decide_approval(
                tenant_id, approval_id, "system", False, "CrewAI 委派失败，转人工对账（孤儿清理）",
                time.time(),
            )
            self.store.update_operation(
                tenant_id, operation_id, OperationStatus.HUMAN_HANDOFF,
                {"error": str(exc), "message": reason},
            )
        except Exception:  # noqa: BLE001 —— 回滚兜底：不因回滚失败掩盖原始委派异常。
            # 若 decide_approval 失败（如 approval 不存在），至少把 operation 置为转人工收口。
            try:
                self.store.update_operation(
                    tenant_id, operation_id, OperationStatus.HUMAN_HANDOFF,
                    {"error": str(exc), "message": reason},
                )
            except Exception:  # noqa: BLE001
                pass
        # 审计留痕。
        try:
            self.store.append_audit(
                tenant_id, ctx.get("user_id", ""), "crewai.orphan_write_rolled_back",
                "operation", operation_id,
                {"approval_id": approval_id, "error": str(exc)}, time.time(),
            )
        except Exception:  # noqa: BLE001
            pass

    @property
    def mock_calls(self) -> list[dict]:
        """最近一次确定性后端的调用链记录（供测试断言）。"""
        return self._mock.calls


def build_crewai_router(settings, adapter: Optional[EcommerceAdapter] = None,
                        store=None) -> CrewAIToolRouter:
    """按配置构造 CrewAI 工具路由（默认关闭，走确定性后端）。

    - `store`（可选）：供写工具创建待审批 operation/approval；主图接入时应传入。
    - 自托管 LLM 配置来自 settings（llm_base_url / llm_model / 网络白名单）。
    """
    from src.tools import build_adapter as _build_adapter
    return CrewAIToolRouter(
        enabled=bool(getattr(settings, "crewai_enabled", False)),
        llm=None,
        adapter=adapter or _build_adapter(settings),
        store=store,
        settings=settings,
        allow_delegation=bool(getattr(settings, "crewai_allow_delegation", True)),
    )
