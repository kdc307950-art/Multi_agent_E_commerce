"""电商能力工具适配器（带租户校验）。

把"外部电商能力"（当前为 mock 数据源，可替换为真实电商 API）封装为带租户/归属/资格的
工具适配器。所有业务节点/子智能体必须经由本适配器访问订单/物流/资格，禁止直接读
mock_data 或绕过租户作用域。

安全性：
- tenant_id 只能来自调用方认证上下文，绝不由工具参数传入；
- 归属：订单必须属于当前租户；customer 仅能访问本人订单；agent/admin/approver 可访问本租户订单；
- 资格：退款需已签收 + 金额合法 + 窗口内；退货需窗口内 + 商品类别可退；改址需订单归属 + 地址参数合法；
- 资格无法确定（窗口超限、未签收、类别不支持、金额非法）一律返回 eligible=False + 原因，
  由节点 fail-closed 转人工，绝不自动放行。

该适配器不调用任何第三方 SaaS；数据源完全自托管（mock / 项目自管电商 API）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from src.llm.validation import validate_address_change, validate_order_ref
from src.tools import mock_data
from src.execution.engine import ExecutionEngine
from src.execution.provider import FundsProvider
from src.execution.types import ExecutionMode, ExecutionOutcome, ExecutionStatus

# 不可退类别（生鲜/定制不支持无理由退货；退款亦需人工核验）
NON_RETURNABLE_CATEGORIES: set[str] = {"fresh", "custom"}
# 写权限角色（可跨本人操作本租户订单）
STAFF_ROLES: set[str] = {"agent", "admin", "approver"}


class AdapterError(Exception):
    """适配器业务错误，携带机器可读代码（供节点 fail-closed 记录）。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class OrderRecord:
    tenant_id: str
    order_id: str
    user_id: str
    status: str
    total_amount: float
    items: list[dict] = field(default_factory=list)
    carrier: str = ""
    tracking_no: str = ""
    created_at: Optional[float] = None
    delivered_at: Optional[float] = None

    @property
    def categories(self) -> set[str]:
        return {i.get("category", "") for i in self.items}


@dataclass(frozen=True)
class EligibilityResult:
    eligible: bool
    reason: Optional[str] = None
    amount: Optional[float] = None
    detail: Optional[str] = None


class EcommerceAdapter:
    """带租户/归属/资格校验的电商能力适配器 + 审批后执行面。

    执行接口（execute_operation）把批准后的退款/退货/改址交给 ExecutionEngine，
    通过 shadow/live 状态机、补偿、回调验签与对账任务收口；所有敏感执行仍只由
    唯一 human_approval 触发。tenant_id/user_id/role 一律来自服务端 TenantContext。
    """

    def __init__(self, *, refund_window_days: int = 7, return_window_days: int = 30,
                 execution_mode: ExecutionMode = ExecutionMode.SHADOW,
                 callback_secret: str = "", provider: FundsProvider | None = None,
                 confirm_timeout_seconds: float = 900.0) -> None:
        self._refund_window = refund_window_days
        self._return_window = return_window_days
        self._execution_mode = execution_mode
        self._callback_secret = callback_secret
        self._provider = provider
        self._confirm_timeout = confirm_timeout_seconds

    # ---- 订单/物流（带归属校验）----
    def get_order(self, tenant_id: str, user_id: str, role: str, order_id: str) -> OrderRecord:
        order_id = validate_order_ref(order_id).order_id  # 非法格式 → LLMOutputError
        raw = mock_data.get_order(tenant_id, order_id)
        if raw is None:
            raise AdapterError("order_not_found", f"订单 {order_id} 不存在或不属于本租户")
        self._assert_ownership(raw, tenant_id, user_id, role)
        return self._to_record(raw)

    def get_shipping(self, tenant_id: str, user_id: str, role: str,
                     order_id: str) -> Optional[dict]:
        order_id = validate_order_ref(order_id).order_id
        # 归属校验（不存在的订单也返回 None，规避信息泄露；写路径已单独拒绝）。
        try:
            self.get_order(tenant_id, user_id, role, order_id)
        except (AdapterError, Exception):
            return None
        return mock_data.get_shipping(tenant_id, order_id)

    # ---- 资格 ----
    def check_refund_eligibility(self, tenant_id: str, user_id: str, role: str,
                                 order_id: str) -> EligibilityResult:
        order = self.get_order(tenant_id, user_id, role, order_id)
        if order.status != "delivered":
            return EligibilityResult(False, "order_not_delivered",
                                     detail="订单未签收，退款资格不明")
        if order.delivered_at is None or (time.time() - order.delivered_at) > self._refund_window * 86400:
            return EligibilityResult(False, "refund_window_exceeded",
                                     detail="超出退款窗口，退款资格需人工核验")
        if order.total_amount <= 0:
            return EligibilityResult(False, "invalid_refund_amount",
                                     detail="订单金额非法，需人工核验")
        if order.categories & NON_RETURNABLE_CATEGORIES:
            return EligibilityResult(False, "category_not_refundable",
                                     detail="商品类别不支持自动退款，需人工核验")
        return EligibilityResult(True, amount=order.total_amount, detail="退款资格确认")

    def check_return_eligibility(self, tenant_id: str, user_id: str, role: str,
                                 order_id: str) -> EligibilityResult:
        order = self.get_order(tenant_id, user_id, role, order_id)
        if order.status != "delivered":
            return EligibilityResult(False, "order_not_delivered",
                                     detail="订单未签收，退货资格不明")
        if order.delivered_at is None or (time.time() - order.delivered_at) > self._return_window * 86400:
            return EligibilityResult(False, "return_window_exceeded",
                                     detail="超出退货窗口，退货资格需人工核验")
        if order.categories & NON_RETURNABLE_CATEGORIES:
            return EligibilityResult(False, "category_not_returnable",
                                     detail="商品类别不支持无理由退货，需人工核验")
        return EligibilityResult(True, detail="退货资格确认")

    def validate_address_change(self, tenant_id: str, user_id: str, role: str,
                                order_id: str, address: dict) -> dict:
        """改址：订单归属 + 地址参数合法。返回规范化后的地址。"""
        order = self.get_order(tenant_id, user_id, role, order_id)
        if not order:
            raise AdapterError("order_not_found", "订单不存在或不属于本租户")
        params = validate_address_change({"order_id": order_id, **address})
        return params.model_dump()

    # ---- 执行（审批后；tenant_id/user_id/role 仍取自服务端 TenantContext）----
    @property
    def execution_mode(self) -> ExecutionMode:
        return self._execution_mode

    @property
    def live_execution_enabled(self) -> bool:
        """受控执行开关：仅当 execution_mode=live 且提供方已配置时才开启真实调用。"""
        return self._execution_mode is ExecutionMode.LIVE and self._provider is not None

    def make_execution_engine(self, store) -> ExecutionEngine:
        """按适配器配置构造执行引擎（供回调端点/对账任务复用同一模式与密钥）。"""
        return ExecutionEngine(
            store,
            mode=self._execution_mode,
            provider=self._provider,
            callback_secret=self._callback_secret,
            confirm_timeout_seconds=self._confirm_timeout,
        )

    def execute_operation(self, store, tenant_id: str, user_id: str, role: str,
                          operation_id: str, *, mode: ExecutionMode | None = None,
                          provider: FundsProvider | None = None) -> ExecutionOutcome:
        """执行审批通过的操作（幂等：重复 execution 重放，不重复提交外部）。

        - 先按操作加载并复核归属（订单属于当前租户；customer 仅本人；staff 本租户）。
        - 交由 ExecutionEngine 走状态机；shadow/live 由 adapter 配置或单次覆盖。
        """
        op = store.get_operation(tenant_id, operation_id)
        if op.tenant_id != tenant_id:
            raise AdapterError("cross_tenant_denied", "跨租户访问被拒绝")
        # 复核订单归属（防御在depth：订单必须属当前租户，且与使用者权限匹配）。
        order = self.get_order(tenant_id, user_id, role, op.order_id)
        engine = ExecutionEngine(
            store,
            mode=mode or self._execution_mode,
            provider=provider or self._provider,
            callback_secret=self._callback_secret,
            confirm_timeout_seconds=self._confirm_timeout,
        )
        return engine.execute(op, order)

    # ---- 内部 ----
    def _assert_ownership(self, raw: dict, tenant_id: str, user_id: str, role: str) -> None:
        if raw["tenant_id"] != tenant_id:
            raise AdapterError("cross_tenant_denied", "跨租户访问被拒绝")
        if raw["user_id"] != user_id and role not in STAFF_ROLES:
            raise AdapterError("order_not_owned", "无权访问该订单")

    @staticmethod
    def _to_record(raw: dict) -> OrderRecord:
        return OrderRecord(
            tenant_id=raw["tenant_id"], order_id=raw["order_id"], user_id=raw["user_id"],
            status=raw["status"], total_amount=raw["total_amount"], items=raw.get("items", []),
            carrier=raw.get("carrier", ""), tracking_no=raw.get("tracking_no", ""),
            created_at=raw.get("created_at"), delivered_at=raw.get("delivered_at"),
        )


def build_adapter(settings) -> EcommerceAdapter:
    """按配置构造电商适配器（资格窗口可配置；默认 7/30 天；执行开关 shadow/live 可配置）。"""
    from src.execution import build_provider as _build_provider
    from src.execution.types import ExecutionMode as _Mode

    mode_name = str(getattr(settings, "execution_mode", "shadow")).strip().lower()
    mode = _Mode.LIVE if mode_name == "live" else _Mode.SHADOW
    provider = _build_provider(settings) if mode is _Mode.LIVE else None
    return EcommerceAdapter(
        refund_window_days=int(getattr(settings, "refund_eligibility_days", 7)),
        return_window_days=int(getattr(settings, "return_eligibility_days", 30)),
        execution_mode=mode,
        callback_secret=str(getattr(settings, "execution_callback_hmac_secret", "")),
        provider=provider,
        confirm_timeout_seconds=float(getattr(settings, "execution_confirm_timeout_seconds", 900.0)),
    )
