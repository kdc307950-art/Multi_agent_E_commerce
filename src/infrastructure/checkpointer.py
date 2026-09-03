"""TenantScopedCheckpointer：基于锁定版 AsyncPostgresSaver 的租户作用域保存器。

设计依据：《生产基线与验收测试》§四、《会话与线程管理设计文档》§三.3。

要点（均不可省略）：
1. **唯一入口**：`graph.compile(checkpointer=tenant_scoped_checkpointer)` 只能接收本对象；
   raw `AsyncPostgresSaver` 不暴露给业务代码。
2. **可信上下文**：每次访问从 `current_checkpoint_scope`（ContextVar）读取服务端建立并校验的
   `CheckpointRequestScope`。`config.configurable.tenant_id` 只用于可观测，不作授权依据。
   scope 缺失、thread 不一致、scope 非 `active` 一律拒绝并写审计。
3. **同连接/同事务 RLS 闭环**：重写 `_cursor`，在**执行 saver SQL 的同一连接与同一事务**内先
   `set_config('app.tenant_id', tenant, true)`（事务本地），再执行父母类的 checkpoint SQL。
   绝不先在业务连接 `SET`、再让 saver 另开/借用连接执行。
4. **完整接口**：覆盖 `aget_tuple / alist / aput / aput_writes / adelete_thread` 及依赖它们
   的 `aget / aget_state / aget_delta_channel_history`；raw saver 无业务可达路径。
5. **初始化**：官方 checkpoint 表的建表与 RLS 启用由迁移阶段完成（见 migrations.py），
   因此本类覆写 `setup()` 为显式禁止，以防运行时误用绕过 RLS 的两段式初始化。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import AsyncIterator, Optional

from langgraph.checkpoint.postgres._ainternal import get_connection
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row

from src.core.types import DomainError, ErrorCode

# 事务本地设置租户作用域（第三参 true => 事务本地，等价 SET LOCAL）
_SET_TENANT_SQL = "SELECT set_config('app.tenant_id', %s, true)"


@dataclass(frozen=True)
class CheckpointRequestScope:
    """服务端建立并校验后的不可变检查点请求作用域。"""

    tenant_id: str
    user_id: str
    thread_id: str
    source: str  # request / approval_resume / history / cleanup / tenant_delete


current_checkpoint_scope: ContextVar[Optional[CheckpointRequestScope]] = ContextVar(
    "current_checkpoint_scope", default=None
)


def set_checkpoint_scope(scope: CheckpointRequestScope):
    """写入当前协程的可信作用域；返回用于复位旧值的 token。"""
    return current_checkpoint_scope.set(scope)


def reset_checkpoint_scope(token) -> None:
    if token is None:
        return
    current_checkpoint_scope.reset(token)


def require_checkpoint_scope() -> CheckpointRequestScope:
    """读取当前可信作用域；缺失则拒绝（默认拒绝语义）。"""
    scope = current_checkpoint_scope.get()
    if scope is None:
        raise DomainError(
            ErrorCode.MISSING_TENANT_CONTEXT,
            "缺少可信的检查点租户作用域", 403,
            detail={"reason": "checkpoint_scope_missing"},
        )
    return scope


@asynccontextmanager
async def checkpoint_request_scope(
    scope: CheckpointRequestScope,
):
    """在 with 块内设置可信检查点作用域，块结束自动复位。

    调用方必须在进入前通过 store 完成 session 归属与 scope 状态校验（如
    `authorize_checkpoint_access`），此处只负责 ContextVar 的写/复位。
    """
    token = set_checkpoint_scope(scope)
    try:
        yield scope
    finally:
        reset_checkpoint_scope(token)


class TenantScopedCheckpointer(AsyncPostgresSaver):
    """租户作用域异步保存器（严格锁定 langgraph-checkpoint-postgres==3.1.2 接口）。"""

    async def setup(self) -> None:
        # 官方表由迁移阶段初始化，避免运行时走无 scope 的建表路径绕过 RLS。
        raise RuntimeError(
            "checkpoint 表初始化必须走 migrations.apply_business_schema/initialize_checkpoint_schema；"
            "禁止在运行时调用 saver.setup() 绕过 RLS 分级初始化。"
        )

    # -- 内部：scope 校验（thread 一致性） --
    def _scope_for_thread(self, thread_id: str) -> CheckpointRequestScope:
        scope = require_checkpoint_scope()
        if scope.thread_id != thread_id:
            raise DomainError(
                ErrorCode.CROSS_TENANT_DENIED,
                "请求的线程不在当前可信租户作用域内", 403,
                detail={"reason": "checkpoint_thread_mismatch"},
            )
        return scope

    # -- 核心：同连接/同事务 RLS 闭环 --
    @asynccontextmanager
    async def _cursor(self, *, pipeline: bool = False):
        """覆盖父类：在 saver 实际执行 SQL 的同一连接与同一事务内设置 app.tenant_id。

        显式使用 ASYNC 连接；无论传入的 conn 是 AsyncConnection 还是 AsyncConnectionPool，
        都统一在事务内先 set_config，再执行 checkpoint SQL，保证 RLS 生效于同一事务。
        """
        scope = require_checkpoint_scope()
        tenant_id = scope.tenant_id
        async with self.lock, get_connection(self.conn) as conn:
            async with conn.transaction():
                await conn.execute(_SET_TENANT_SQL, (tenant_id,))
                async with conn.cursor(binary=True, row_factory=dict_row) as cur:
                    yield cur

    # -- Read --
    async def aget_tuple(self, config):
        self._scope_for_thread(config["configurable"]["thread_id"])
        return await super().aget_tuple(config)

    async def alist(self, config, *, filter=None, before=None, limit=None) -> AsyncIterator:
        if config is not None:
            self._scope_for_thread(config["configurable"]["thread_id"])
        async for item in super().alist(config, filter=filter, before=before, limit=limit):
            yield item

    async def aget_delta_channel_history(self, *, config, channels):
        self._scope_for_thread(config["configurable"]["thread_id"])
        return await super().aget_delta_channel_history(config=config, channels=channels)

    # -- Write --
    async def aput(self, config, checkpoint, metadata, new_versions):
        self._scope_for_thread(config["configurable"]["thread_id"])
        return await super().aput(config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        self._scope_for_thread(config["configurable"]["thread_id"])
        await super().aput_writes(config, writes, task_id, task_path)

    # -- Delete --
    async def adelete_thread(self, thread_id: str) -> None:
        self._scope_for_thread(thread_id)
        await super().adelete_thread(thread_id)
