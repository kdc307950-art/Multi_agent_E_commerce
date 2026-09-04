"""领域类型与冻结契约映射。

安全红线（不可违反）：
- tenant_id / user_id 一律来自服务端认证后的 TenantContext，客户端不可覆盖。
- platform_admin 是独立平台级能力，不是租户成员角色。
- 退款/退货/改址必须经过唯一 human_approval，无 direct -> execute 绕过。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------
class Role(str, Enum):
    CUSTOMER = "customer"
    AGENT = "agent"
    ADMIN = "admin"
    APPROVER = "approver"

    @classmethod
    def tenant_roles(cls) -> set[str]:
        """租户成员角色；platform_admin 不属于此处。"""
        return {r.value for r in cls}


class TenantStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETING = "deleting"


class MembershipStatus(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"


class PendingAction(str, Enum):
    REFUND = "refund"
    RETURN_REQUEST = "return_request"
    RETURN_ADDRESS = "return_address"
    POLICY = "policy"
    OTHER = "other"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    TIMEOUT = "timeout"


class OperationStatus(str, Enum):
    PENDING = "pending"          # 待审批
    EXECUTED = "executed"        # 已执行
    FAILED = "failed"            # 执行失败（可重试/补偿）
    REJECTED = "rejected"        # 审批拒绝
    HUMAN_HANDOFF = "human_handoff"  # 转人工


class SessionStatus(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    DELETING = "deleting"


class SSEEventType(str, Enum):
    ACCEPTED = "accepted"
    TOKEN = "token"
    NODE = "node"
    APPROVAL_REQUIRED = "approval_required"
    DONE = "done"
    ERROR = "error"


# 冻结的六种 SSE 事件（AI 流唯一允许的 event 名）
FROZEN_SSE_EVENTS = {e.value for e in SSEEventType}


# 业务操作幂等键前缀（唯一键含 tenant_id，绝不含重试次数/attempt）
OPERATION_PREFIX = {
    PendingAction.REFUND: "oprefund",
    PendingAction.RETURN_REQUEST: "opreturn",
    PendingAction.RETURN_ADDRESS: "opaddr",
}


# ---------------------------------------------------------------------------
# 领域对象（内部，不作为 API 序列化契约）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TenantContext:
    """服务端认证后的不可变租户上下文。role 只能来自租户成员关系。"""

    tenant_id: str
    user_id: str
    role: Role
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def check_role(self, allowed: set[str]) -> bool:
        return self.role.value in allowed


@dataclass(frozen=True)
class Tenant:
    id: str
    name: str
    status: TenantStatus = TenantStatus.ACTIVE
    created_at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class Membership:
    tenant_id: str
    user_id: str
    role: Role
    status: MembershipStatus = MembershipStatus.ACTIVE


@dataclass(frozen=True)
class Session:
    thread_id: str
    tenant_id: str
    user_id: str
    created_at: float
    expires_at: float
    last_active_at: float
    status: SessionStatus = SessionStatus.ACTIVE
    message_count: int = 0
    title: Optional[str] = None


@dataclass(frozen=True)
class Approval:
    approval_id: str
    tenant_id: str
    thread_id: str
    operation_id: str
    pending_action: PendingAction
    status: ApprovalStatus
    created_at: float
    order_id: Optional[str] = None
    amount: Optional[float] = None
    reason: Optional[str] = None
    approver: Optional[str] = None
    feedback: Optional[str] = None
    decided_at: Optional[float] = None


@dataclass(frozen=True)
class Operation:
    operation_id: str
    tenant_id: str
    thread_id: str
    order_id: str
    pending_action: PendingAction
    idempotency_key: str
    status: OperationStatus
    created_at: float
    result: Optional[dict] = None


@dataclass(frozen=True)
class StreamEvent:
    """持久化的 SSE 事件；id 在一次 stream 内严格递增。"""

    stream_id: str
    seq: int
    event: str
    data: dict
    created_at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class AuditRecord:
    audit_id: str
    tenant_id: str
    user_id: str
    action: str
    target_type: str
    target_id: str
    detail: dict
    created_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# 错误码（机器可读，用于 SSE error 事件与业务记录）
# ---------------------------------------------------------------------------
class ErrorCode(str, Enum):
    MISSING_TENANT_CONTEXT = "missing_tenant_context"
    TENANT_SUSPENDED = "tenant_suspended"
    CROSS_TENANT_DENIED = "cross_tenant_denied"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    INVALID_SSE_EVENT = "invalid_sse_event"
    INVALID_RESUME = "invalid_resume"
    STREAM_EXPIRED = "stream_expired"
    SESSION_EXPIRED = "session_expired"        # 会话已过期 → 拒绝访问/使用
    SESSION_DELETING = "session_deleting"      # 会话删除中 → 拒绝访问/使用
    AUTH_BACKEND_DISABLED = "auth_backend_disabled"  # 受限环境禁用 Mock 认证，fail-closed
    TOO_MANY_REQUESTS = "too_many_requests"      # 登录限流/退避触发 → HTTP 429
    FAIL_CLOSED_RAG = "fail_closed_rag"
    MODEL_NOT_IN_WHITELIST = "model_not_in_whitelist"
    IDEMPOTENCY_REPLAY = "idempotency_replay"
    APPROVAL_ALREADY_DECIDED = "approval_already_decided"
    APPROVAL_TIMEOUT = "approval_timeout"      # 审批超时未决策 → 转人工
    APPROVAL_BINDING_MISMATCH = "approval_binding_mismatch"  # 绑定不一致（operation/action/thread）
    EXECUTION_FAILED = "execution_failed"      # 执行节点失败 → 保留 op 转人工
    INTERNAL_ERROR = "internal_error"


class DomainError(Exception):
    """领域错误，携带机器可读错误码与 HTTP 状态码。"""

    def __init__(self, code: ErrorCode, message: str, status_code: int = 400, detail: Optional[dict] = None):
        self.code = code.value
        self.message = message
        self.status_code = status_code
        self.detail = detail or {}
        super().__init__(message)


def generate_thread_id() -> str:
    """不透明、不可猜测的 thread_id（不嵌入 user_id）。"""
    return str(uuid.uuid4())


def generate_operation_key(action: PendingAction, tenant_id: str, order_id: str, request_id: str) -> str:
    """业务幂等键：租户内唯一，绝不含重试次数/attempt。"""
    prefix = OPERATION_PREFIX[action]
    return f"{prefix}:{tenant_id}:{order_id}:{request_id}"
