"""会话/检查点生命周期：滑动 7 天保留、过期清理、删除与失败重试。

设计依据：《会话与线程管理设计文档》§二.2 / §三.4 / §七.3。

流程（任何清理都必须遵守，禁止先删 scope/session 再删 checkpoint）：
1. `claim_expired_scopes_for_cleanup`：以 `FOR UPDATE SKIP LOCKED` 占用已过期且 `active` 的 scope，
   原子标记为 `deleting`（并递增 attempts），阻断新的图执行/恢复。
2. 对每个候选，在 `checkpoint_request_scope(source="cleanup")` 下调用
   `tenant_scoped_checkpointer.adelete_thread(thread_id)` 删除官方 checkpoint/blob/write。
3. 仅当删除成功，才 `finish_cleanup_scope`（删除 scope + session + 级别联 + 审计）。
4. 失败：保留 `deleting` scope、`record_cleanup_failure`（递增尝试 + 审计/告警），下一轮幂等重试。

保留策略：合法国：每次 `start` / 审批续跑 / 合法历史恢复都在同一受控事务内更新
`session.last_active_at/expires_at` 与 `scope.last_active_at/expires_at`（滑动 7 天）；
断线重连只续传事件，不自动延长 TTL。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from src.infrastructure.checkpointer import (
    CheckpointRequestScope,
    checkpoint_request_scope,
)

DEFAULT_TTL_DAYS = 7


@dataclass
class CleanupResult:
    claimed: int = 0
    deleted: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def build_cleanup_scope(tenant_id: str, thread_id: str) -> CheckpointRequestScope:
    """构造清理用的系统级 scope（source=cleanup，只允许清理所引用的 thread）。"""
    return CheckpointRequestScope(
        tenant_id=tenant_id, user_id="SYSTEM", thread_id=thread_id, source="cleanup",
    )


async def cleanup_expired_threads(
    store,
    checkpointer,
    *,
    now: Optional[float] = None,
    batch_size: int = 100,
    claim_engine=None,
) -> CleanupResult:
    """清理已过期会话；失败保留 deleting scope 并递增重试，幂等可重入。

    `claim_engine`：系统级连接（可绕过 RLS），用于跨租户扫描全部过期 scope；
    缺省时使用 store 自身 engine。每个候选取得后仍以该租户的 `app.tenant_id`
    作用域删除 checkpoint 与 scope/session。
    """
    now = now if now is not None else time.time()
    candidates = store.claim_expired_scopes_for_cleanup(now, batch_size=batch_size,
                                                        engine=claim_engine)
    result = CleanupResult(claimed=len(candidates))
    for candidate in candidates:
        tenant_id = candidate["tenant_id"]
        thread_id = candidate["thread_id"]
        scope = build_cleanup_scope(tenant_id, thread_id)
        try:
            async with checkpoint_request_scope(scope):
                await checkpointer.adelete_thread(thread_id)
            # 只有 checkpoint 删除成功，才删 scope/session（同事务 + 审计）。
            store.finish_cleanup_scope(tenant_id, thread_id, now)
            result.deleted += 1
        except Exception as exc:  # 保留 deleting scope、递增尝试、写审计；下一轮重试。
            store.record_cleanup_failure(tenant_id, thread_id, exc, now)
            result.failed += 1
            result.errors.append(f"{tenant_id}:{thread_id}: {exc}")
    return result
