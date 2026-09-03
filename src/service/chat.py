"""聊天流式服务：把消息交给 LangGraph 主图，映射为冻结的六种 SSE 事件。

冻结契约（见《生产环境架构设计》§3）：
- 每个 SSE 帧含严格递增 id、六选一 event、单行 JSON data。
- start 以 (tenant_id, user_id, client_request_id) 原子去重；同请求重投只重放，不新建图执行。
- resume 只重放持久化事件并附着既有流，绝不重跑图/工具/敏感写。
- 只把 LangGraph 事件映射为本节的六种事件，不透传内部事件。
"""
from __future__ import annotations

import json
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from src.core.types import (
    DomainError,
    ErrorCode,
    FROZEN_SSE_EVENTS,
)
from src.graph.builder import build_graph
from src.observability.tracing import trace_span


@asynccontextmanager
async def _maybe_scope(scope):
    """若提供了可信检查点作用域则建立（checkpoint RLS 同连接/同事务），否则直通。"""
    if scope is None:
        yield None
    else:
        from src.infrastructure.checkpointer import checkpoint_request_scope
        async with checkpoint_request_scope(scope):
            yield scope


def _now() -> float:
    return time.time()


def format_sse_frame(seq: int, event: str, data: dict) -> str:
    """生成一个符合契约的 SSE 帧：id/event/单行 JSON data。"""
    if event not in FROZEN_SSE_EVENTS:
        raise DomainError(ErrorCode.INVALID_SSE_EVENT, f"非法 SSE 事件: {event}", 500)
    payload = json.dumps(data, ensure_ascii=False)
    return f"id: {seq}\nevent: {event}\ndata: {payload}\n\n"


class ChatRunner:
    def __init__(self, store, llm, checkpointer, retriever=None, adapter=None,
                 conf_threshold=0.7) -> None:
        self.store = store
        self.llm = llm
        self.graph = build_graph(llm, store, checkpointer, retriever=retriever,
                                 adapter=adapter, conf_threshold=conf_threshold)
        self._config = {"configurable": {"thread_id": None}}

    def _scoped_checkpoint_scope(self, ctx, thread_id: str, ttl_days: int):
        """为支持 scope 的 store（Postgres 数据面）建立可信检查点作用域。

        memory/sqlite 后端（InMemorySaver）没有该项，返回 None，保持现状。
        """
        if not hasattr(self.store, "authorize_checkpoint_access"):
            return None
        from src.infrastructure.checkpointer import CheckpointRequestScope
        auth = self.store.authorize_checkpoint_access(
            ctx.tenant_id, ctx.user_id, ctx.role.value, thread_id, _now(), ttl_days,
            source="request",
        )
        return CheckpointRequestScope(**auth)

    async def run_start(self, ctx, thread_id: str, client_request_id: str, message: str,
                        session_ttl_days: int) -> AsyncIterator[str]:
        # 会话归属校验已由路由层完成（start 前 validate_session_owner，返回 404）。
        # start 原子去重：同租户同用户同 client_request_id 重投只重放。
        existing = self.store.find_stream_by_client(ctx.tenant_id, ctx.user_id, client_request_id)
        if existing:
            async for frame in self._replay(ctx, existing, 0):
                yield frame
            return

        stream_id = str(uuid.uuid4())
        self.store.create_stream(stream_id, ctx.tenant_id, ctx.user_id, thread_id,
                                 client_request_id, "start", _now())
        self.store.touch_session(ctx.tenant_id, thread_id, _now(), session_ttl_days)
        self.store.increment_message_count(ctx.tenant_id, thread_id, 1)

        # accepted（第一帧）
        seq = self.store.append_event(ctx.tenant_id, stream_id, "accepted", {
            "stream_id": stream_id, "thread_id": thread_id,
            "client_request_id": client_request_id, "mode": "start",
        }, _now())
        yield format_sse_frame(seq, "accepted", {
            "stream_id": stream_id, "thread_id": thread_id,
            "client_request_id": client_request_id, "mode": "start",
        })

        config = {"configurable": {"thread_id": thread_id}}
        input_state = {
            "messages": [{"role": "user", "content": message}],
            "tenant_id": ctx.tenant_id,
            "user_id": ctx.user_id,
            "role": ctx.role.value,
            "thread_id": thread_id,
            "client_request_id": client_request_id,
            "model": self.llm.model,
        }

        # 可信检查点作用域（Postgres 数据面：RLS 与 saver SQL 同连接/同事务）。
        cp_scope = self._scoped_checkpoint_scope(ctx, thread_id, session_ttl_days)

        # 用 astream_events 监听节点进度并映射为 node 事件（不透传内部事件）。
        # LangGraph 用 on_chain_start/end + metadata["langgraph_node"] 标记图节点。
        seen_nodes: set[str] = set()
        try:
            async with _maybe_scope(cp_scope):
                # 自托管观测：整段图执行包在 Langfuse span 内，metadata 写 tenant/session，
                # 输入消息经脱敏（地址/支付/订单号不入明文 trace）。未配置 Langfuse 时 no-op。
                with trace_span("chat.run", tenant_id=ctx.tenant_id, session_id=thread_id,
                                user_id=ctx.user_id,
                                input={"message": message,
                                       "client_request_id": client_request_id}):
                    async for event in self.graph.astream_events(input_state, config=config, version="v2"):
                        ev = event.get("event")
                        meta = event.get("metadata") or {}
                        node = meta.get("langgraph_node") if isinstance(meta, dict) else None
                        if not node:
                            continue
                        emitted = status_to_emit = None
                        if ev == "on_chain_start" and node not in seen_nodes:
                            seen_nodes.add(node)
                            emitted, status_to_emit = node, "running"
                        elif ev == "on_chain_end" and node in seen_nodes:
                            seen_nodes.discard(node)
                            emitted, status_to_emit = node, "done"
                        if emitted:
                            seq = self.store.append_event(ctx.tenant_id, stream_id, "node",
                                                          {"name": emitted, "status": status_to_emit}, _now())
                            yield format_sse_frame(seq, "node", {"name": emitted, "status": status_to_emit})
        except Exception:  # 图执行可能因中断/超时等结束，不代表失败；中断由 aget_state 反映。
            pass

        # 读取最终状态判断是否触发审批中断。
        async with _maybe_scope(cp_scope):
            snapshot = await self.graph.aget_state(config)
        values = snapshot.values or {}
        interrupt_data = values.get("__interrupt__")

        pending_action = values.get("pending_action")
        if pending_action and values.get("needs_approval"):
            approval_id = values.get("approval_id")
            operation_id = values.get("operation_id")
            seq = self.store.append_event(ctx.tenant_id, stream_id, "approval_required", {
                "approval_id": approval_id, "operation_id": operation_id, "status": "pending",
            }, _now())
            yield format_sse_frame(seq, "approval_required", {
                "approval_id": approval_id, "operation_id": operation_id, "status": "pending",
            })
            return

        # 正常结束：token + done
        final_response = values.get("final_response")
        if final_response:
            seq = self.store.append_event(ctx.tenant_id, stream_id, "token",
                                          {"delta": final_response}, _now())
            yield format_sse_frame(seq, "token", {"delta": final_response})

        operation_id = values.get("operation_id")
        if values.get("falls_to_error"):
            seq = self.store.append_event(ctx.tenant_id, stream_id, "error", {
                "code": values.get("reason") or ErrorCode.INTERNAL_ERROR.value,
                "message": "当前请求需要人工处理。",
                "retryable": False,
                "operation_id": operation_id,
            }, _now())
            yield format_sse_frame(seq, "error", {
                "code": values.get("reason") or ErrorCode.INTERNAL_ERROR.value,
                "message": "当前请求需要人工处理。",
                "retryable": False,
                "operation_id": operation_id,
            })
            return

        seq = self.store.append_event(ctx.tenant_id, stream_id, "done", {
            "status": "completed", "final_response": final_response, "operation_id": operation_id,
        }, _now())
        yield format_sse_frame(seq, "done", {
            "status": "completed", "final_response": final_response, "operation_id": operation_id,
        })

    async def run_resume(self, ctx, stream_id: str, last_event_id: int) -> AsyncIterator[str]:
        """续传：只重放 last_event_id 之后的事件，绝不重跑图/工具/敏感写。"""
        async for frame in self._replay(ctx, stream_id, last_event_id):
            yield frame

    async def _replay(self, ctx, stream_id: str, last_event_id: int) -> AsyncIterator[str]:
        # 重放前必须校验流归属与会话可用性（跨用户/过期/删除中拒绝并审计）。
        from src.auth.security import validate_stream_owner
        validate_stream_owner(self.store, ctx, stream_id)
        stream = self.store.get_stream(ctx.tenant_id, stream_id)
        if stream["expires_at"] < _now():
            raise DomainError(ErrorCode.STREAM_EXPIRED, "流已过期", 410)
        events = self.store.events_after(ctx.tenant_id, stream_id, last_event_id)
        for e in events:
            yield format_sse_frame(e["seq"], e["event"], e["data"])
