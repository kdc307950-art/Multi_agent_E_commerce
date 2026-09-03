"""后台任务租户绑定与消费前复核。

安全要求（《生产基线与验收测试》§五 / 《Agent 宪法》第二层 §4）：
- 所有 Redis/Celery 任务 payload 必须携带 `tenant_id`（数据面带租户范围）。
- 任务开始执行时**先复核租户状态**（活跃度 + 可选成员关系），不 active 一律拒绝消费并转人工；
  绝不"无租户条件"地执行。
"""
from __future__ import annotations

from typing import Any, Optional

from src.core.types import DomainError, ErrorCode, TenantStatus


def task_payload(tenant_id: str, **data: Any) -> dict:
    """构造带租户作用域的任务 payload；缺失租户上下文的 payload 直接拒绝。"""
    if not tenant_id:
        raise DomainError(ErrorCode.MISSING_TENANT_CONTEXT, "任务缺少租户作用域", 403,
                          detail={"reason": "task_missing_tenant"})
    return {"tenant_id": tenant_id, **data}


def require_tenant_active(store, tenant_id: str, user_id: Optional[str] = None):
    """消费前复核租户状态；租户停用或成员无效一律拒绝（默认拒绝语义）。

    返回活跃租户对象。校验失败抛 DomainError，调用方应把任务标记为失败/转人工，
    而不是用默认租户或全局连接继续执行。
    """
    tenant = store.get_tenant(tenant_id)  # 不存在 -> 404
    if tenant.status != TenantStatus.ACTIVE:
        raise DomainError(ErrorCode.TENANT_SUSPENDED, "租户已停用，任务拒绝消费", 403,
                          detail={"reason": "task_tenant_suspended"})
    if user_id is not None:
        store.require_active_membership(tenant_id, user_id)
    return tenant


def enqueue_tenant_task(app, task_name: str, tenant_id: str, *, countdown: int = 0, **data: Any):
    """把任务以带租户 payload 的方式投递到 Celery broker。"""
    payload = task_payload(tenant_id, **data)
    return app.send_task(task_name, args=[payload], kwargs={}, countdown=countdown)
