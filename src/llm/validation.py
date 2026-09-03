"""严格校验：意图分类、工具参数、审批恢复参数、模型输出。

集中放四类校验，供节点/API/LLM 层复用。任何一项校验失败都应 fail-closed 转人工，
而不是把未经验证或畸形数据推进业务流程。

- 模型输出：意图分类、幻觉检测的 schema/值域；
- 工具参数：订单号、退款/退货/改址所需的最小必要字段与量值合法性；
- 审批恢复：interrupt resume/decision 所需的布尔、角色归属与字段类型。
"""
from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from src.llm.base import LLMOutputError

# 意图白名单（与 AgentState.intent 一致）
ALLOWED_INTENTS: set[str] = {
    "order", "shipping", "refund", "return_request",
    "return_address", "policy", "complaint", "other",
}

# 订单号格式（ORD-123或ORD_123，大小写不敏感）
ORDER_ID_RE = re.compile(r"^ORD[-_]?\d+$", re.IGNORECASE)

# 手机号（中国大陆 11 位，去分隔符后校验）
PHONE_RE = re.compile(r"^1[3-9]\d{9}$")


class IntentOutput(BaseModel):
    """意图分类的结构化输出。confidence 必须落在 [0,1]。strict 模式拒绝惰性类型强转。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    intent: Literal["order", "shipping", "refund", "return_request",
                    "return_address", "policy", "complaint", "other"]
    confidence: float = Field(ge=0.0, le=1.0)
    order_id: Optional[str] = None

    @field_validator("order_id")
    @classmethod
    def _check_order_id(cls, v: Optional[str]) -> Optional[str]:
        return v.upper() if v else None


class HallucinationCheck(BaseModel):
    """幻觉检测的结构化输出。strict 模式拒绝惰性类型强转（如 "yes"→True）。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    faithful: bool
    issues: list[str] = Field(default_factory=list)


class OrderRef(BaseModel):
    """订单引用参数：order_id 必须是合法格式。"""

    model_config = ConfigDict(extra="forbid")

    order_id: str

    @field_validator("order_id")
    @classmethod
    def _norm(cls, v: str) -> str:
        v = v.strip().upper()
        if not ORDER_ID_RE.match(v):
            raise ValueError(f"订单号格式非法: {v!r}")
        return v


class AddressChangeParams(BaseModel):
    """改退货地址所需的最小必要字段；缺字段或非法即拒绝。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    order_id: str
    receiver_name: str = Field(min_length=1, max_length=50)
    phone: str
    region: str = Field(min_length=2, max_length=120)   # 省市区
    detail: str = Field(min_length=2, max_length=200)    # 详细地址

    @field_validator("order_id")
    @classmethod
    def _norm_order(cls, v: str) -> str:
        v = v.strip().upper()
        if not ORDER_ID_RE.match(v):
            raise ValueError(f"订单号格式非法: {v!r}")
        return v

    @field_validator("phone")
    @classmethod
    def _norm_phone(cls, v: str) -> str:
        v = re.sub(r"[\s\-]", "", v).strip()
        if not PHONE_RE.match(v):
            raise ValueError("手机号格式非法")
        return v


class RefundRequestParams(BaseModel):
    """退款请求参数：order_id + 合法理由。金额由系统按订单实付计算，不允许模型伪造。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    order_id: str
    reason: str = Field(min_length=1, max_length=200)

    @field_validator("order_id")
    @classmethod
    def _norm_order(cls, v: str) -> str:
        v = v.strip().upper()
        if not ORDER_ID_RE.match(v):
            raise ValueError(f"订单号格式非法: {v!r}")
        return v


class ReturnRequestParams(BaseModel):
    """退货请求参数：order_id + 可选退货原因。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    order_id: str
    reason: Optional[str] = Field(default=None, max_length=200)

    @field_validator("order_id")
    @classmethod
    def _norm_order(cls, v: str) -> str:
        v = v.strip().upper()
        if not ORDER_ID_RE.match(v):
            raise ValueError(f"订单号格式非法: {v!r}")
        return v


class ApprovalResume(BaseModel):
    """人工审批恢复/决策的最小白名单：approved 必须为 bool（strict 拒绝字符串强转）。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    approved: bool
    approver: Optional[str] = None
    feedback: Optional[str] = None


def validate_intent_output(data: dict) -> IntentOutput:
    """校验并规范化意图分类输出；非法抛 LLMOutputError。"""
    try:
        return IntentOutput.model_validate(data)
    except ValidationError as exc:
        raise LLMOutputError(f"意图分类输出非法: {exc}") from exc


def validate_hallucination_check(data: dict) -> HallucinationCheck:
    """校验并规范化幻觉检测输出；非法抛 LLMOutputError。"""
    try:
        return HallucinationCheck.model_validate(data)
    except ValidationError as exc:
        raise LLMOutputError(f"幻觉检测输出非法: {exc}") from exc


def validate_order_ref(order_id: str) -> OrderRef:
    try:
        return OrderRef(order_id=order_id)
    except ValidationError as exc:
        raise LLMOutputError(f"订单号非法: {order_id!r}") from exc


def validate_address_change(data: dict) -> AddressChangeParams:
    try:
        return AddressChangeParams.model_validate(data)
    except ValidationError as exc:
        raise LLMOutputError(f"改址参数非法: {exc}") from exc


def validate_refund_params(data: dict) -> "RefundRequestParams":
    try:
        return RefundRequestParams.model_validate(data)
    except ValidationError as exc:
        raise LLMOutputError(f"退款参数非法: {exc}") from exc


def validate_return_params(data: dict) -> ReturnRequestParams:
    try:
        return ReturnRequestParams.model_validate(data)
    except ValidationError as exc:
        raise LLMOutputError(f"退货参数非法: {exc}") from exc


def validate_resume_params(data: dict) -> dict:
    """校验审批恢复参数：approved 必须为 bool（其余字段可选、类型宽松）。非法抛 LLMOutputError。"""
    if not isinstance(data, dict):
        raise LLMOutputError("审批恢复参数必须为对象")
    try:
        model = ApprovalResume.model_validate(data)
    except ValidationError as exc:
        raise LLMOutputError(f"审批恢复参数非法: {exc}") from exc
    return {
        "approved": model.approved,
        "approver": model.approver,
        "feedback": model.feedback,
    }
