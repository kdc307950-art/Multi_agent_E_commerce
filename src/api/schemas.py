"""API 请求/响应契约（Pydantic）。

冻结契约（见《生产环境架构设计》§3 / 《前端体验与工作台设计》§5）：
- /api/chat 仅接受 mode=start 或 mode=resume（判别式），响应为 text/event-stream。
- 客户端不得传 tenant_id；身份与归属由服务端 TenantContext 决定。
- 审批唯一入口 POST /api/approvals/{approval_id}/decision，requires二次确认。
"""
from __future__ import annotations

from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class ChatStart(BaseModel):
    model_config = ConfigDict(extra="forbid")  # 拒绝客户端覆盖 tenant_id/user_id 等未知字段

    mode: Literal["start"]
    thread_id: str
    client_request_id: str
    message: str


class ChatResume(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["resume"]
    stream_id: str


ChatRequest = Annotated[Union[ChatStart, ChatResume], Field(discriminator="mode")]


class NewSessionResponse(BaseModel):
    thread_id: str
    session_id: str


class SessionRead(BaseModel):
    thread_id: str
    title: Optional[str] = None
    created_at: float
    expires_at: float
    status: str
    message_count: int
    user_id: Optional[str] = None


class SessionMessagesResponse(BaseModel):
    messages: list[dict]


class MemberRead(BaseModel):
    tenant_id: str
    user_id: str
    role: str
    status: str


class OrderItemRead(BaseModel):
    name: str
    sku: Optional[str] = None
    price: Optional[float] = None
    quantity: Optional[int] = 1


class OrderRead(BaseModel):
    order_id: str
    status: str
    total_amount: float
    items: list[OrderItemRead]
    carrier: Optional[str] = None
    tracking_no: Optional[str] = None
    shipping_events: list[dict] = []


class ApprovalRead(BaseModel):
    approval_id: str
    operation_id: str
    thread_id: str
    pending_action: str
    status: str
    order_id: Optional[str] = None
    amount: Optional[float] = None
    reason: Optional[str] = None
    created_at: float
    approver: Optional[str] = None
    feedback: Optional[str] = None


class ApprovalDecisionRequest(BaseModel):
    approved: bool
    feedback: Optional[str] = None
    confirmation: bool = False
    # 客户端回传的绑定信息（可选）：服务端据此复核审批决定与操作/动作/线程一致，
    # 防止把某个 approval 的决策应用到错误 operation / 错误动作 / 错误线程。
    operation_id: Optional[str] = None
    pending_action: Optional[str] = None


class ApprovalDecisionResponse(BaseModel):
    operation_id: str
    status: str
    message: str


class LoginRequest(BaseModel):
    """登录端点（签发真实 JWT）请求。

    `credential` 为服务端配置的登录凭据，仅在服务端做哈希比对，绝不入日志。
    """
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    user_id: str
    credential: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    issued_at: float
    scope: str = "tenant"



class OperationRead(BaseModel):
    operation_id: str
    thread_id: str
    order_id: str
    pending_action: str
    status: str
    idempotency_key: str
    created_at: float
    result: Optional[dict] = None


class AuditRead(BaseModel):
    """审计记录读模型（受控审计查询；detail 已脱敏）。"""

    audit_id: str
    tenant_id: str
    user_id: str
    action: str
    target_type: str
    target_id: str
    detail: dict
    created_at: float


class ExecutionRead(BaseModel):
    """执行记录读模型（幂等锚点 + 状态机 + 回执；供人工/对账核对最小必要字段）。"""

    execution_id: str
    operation_id: str
    order_id: str
    pending_action: str
    idempotency_key: str
    mode: str
    status: str
    amount: Optional[float] = None
    external_txn_id: Optional[str] = None
    created_at: float
    updated_at: float
    confirmed_at: Optional[float] = None
    receipt: Optional[dict] = None
    compensation_status: Optional[str] = None
    last_error: Optional[str] = None
