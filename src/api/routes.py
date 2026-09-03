"""REST 路由（统一 /api 前缀，所有入口服务端校验 TenantContext）。

冻结接口（见《生产基线与验收测试》§3.1 / 《生产环境架构设计》§3.5）：
- POST /api/auth/login（签发真实 JWT；仅 real 后端且已配置 AUTH_LOGIN_CREDENTIALS，否则 fail-closed）
- POST /api/sessions、GET /api/sessions、GET /api/sessions/{thread_id}、GET /api/sessions/{thread_id}/messages
- POST /api/chat（SSE）
- GET /api/approvals、GET /api/approvals/{approval_id}、POST /api/approvals/{approval_id}/decision
- GET /api/operations/{operation_id}
人工升级由后端领域服务创建并通过上述资源呈现，不暴露第二个 /escalate 公共入口。
"""
from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.responses import PlainTextResponse
from langgraph.types import Command

from src.api.deps import get_runner, get_settings_dep, get_store, get_tenant_context
from src.api.schemas import (
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    ApprovalRead,
    AuditRead,
    ChatRequest,
    ExecutionRead,
    LoginRequest,
    LoginResponse,
    MemberRead,
    NewSessionResponse,
    OperationRead,
    OrderRead,
    SessionMessagesResponse,
    SessionRead,
)
from src.auth.security import (
    _redact_detail,
    audit_security_denial,
    issue_login_token,
    require_role,
    validate_session_owner,
    validate_stream_owner,
)
from src.config import get_settings
from src.core.types import (
    ApprovalStatus,
    DomainError,
    ErrorCode,
    Role,
    SessionStatus,
    generate_thread_id,
)
from src.observability.metrics import get_metrics
from src.observability.tracing import trace_span

router = APIRouter()


def _now() -> float:
    return time.time()


def _domain(exc: DomainError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.message)


@asynccontextmanager
async def _checkpoint_scope(store, ctx, thread_id: str, source: str):
    """在图访问（历史恢复 / 审批续跑）前建立可信检查点作用域。

    memory/sqlite 后端（InMemorySaver）无此项，直通；Postgres 数据面在每次图调用前
    校验会话归属、scope 状态并同事务滑动 TTL。绝不把请求端的 tenant_id 当授权依据。
    """
    if not hasattr(store, "authorize_checkpoint_access"):
        yield None
        return
    from src.infrastructure.checkpointer import CheckpointRequestScope, checkpoint_request_scope
    auth = store.authorize_checkpoint_access(
        ctx.tenant_id, ctx.user_id, ctx.role.value, thread_id, _now(),
        get_settings().session_ttl_days, source=source,
    )
    scope = CheckpointRequestScope(**auth)
    async with checkpoint_request_scope(scope):
        yield scope


def _staff_role(ctx) -> bool:
    """agent/admin/approver 可查看租户内全部授权数据；customer 仅看本人。"""
    return ctx.role.value in {"agent", "admin", "approver"}


def _visible_thread_ids(store, ctx) -> set[str] | None:
    """customer → 本人会话 thread 集合；staff → None 表示全租户可见。"""
    if _staff_role(ctx):
        return None
    return {s.thread_id for s in store.list_sessions(ctx.tenant_id, ctx.user_id)}


def _check_visible(store, ctx, thread_id: str) -> None:
    """按角色校验 thread 可见；customer 仅本人会话可见，否则 404（不泄露存在）并审计。"""
    if _staff_role(ctx):
        return
    visible = {s.thread_id for s in store.list_sessions(ctx.tenant_id, ctx.user_id)}
    if thread_id not in visible:
        audit_security_denial(store, ctx.tenant_id, ctx.user_id, "cross_user_resource",
                              "session", thread_id)
        raise DomainError(ErrorCode.NOT_FOUND, "资源不可用或无权限", 404)


def _message_to_dict(msg) -> dict:
    """把 LangChain 消息对象/字典统一序列化为 {role, content}，供前端展示会话历史。"""
    if isinstance(msg, dict):
        out = dict(msg)
        if isinstance(out.get("content"), list):
            out["content"] = _content_to_text(out["content"])
        return out
    role = getattr(msg, "type", None) or getattr(msg, "role", None) or "msg"
    if role == "ai":
        role = "assistant"
    return {"role": role, "content": _content_to_text(getattr(msg, "content", ""))}


def _content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                parts.append(str(b.get("text", "")))
        return "\n".join(parts)
    return str(content) if content is not None else ""


# ---- 登录（签发真实 JWT）----
@router.post("/auth/login", response_model=LoginResponse)
async def login(request: LoginRequest, store=Depends(get_store),
                settings=Depends(get_settings_dep)):
    """登录端点：校验服务端凭据 → 签发真实 JWT（固定 iss/aud/exp，支持密钥轮换）。

    仅 `auth_backend=real` + 已配置 `AUTH_LOGIN_CREDENTIALS` 可用；否则 fail-closed（503）。
    任何失败由 `issue_login_token` 写脱敏审计；本端点负责登录成功留痕（不含凭据）。
    """
    token = issue_login_token(store, settings, request.tenant_id, request.user_id,
                              request.credential)
    store.append_audit(request.tenant_id, request.user_id, "auth.login", "auth",
                       f"{request.tenant_id}:{request.user_id}",
                       {"role": "tenant"}, _now())
    return LoginResponse(access_token=token, expires_in=settings.auth_jwt_ttl_seconds,
                         issued_at=_now())


# ---- 会话 ----
@router.post("/sessions", response_model=NewSessionResponse, status_code=201)
async def create_session(ctx=Depends(get_tenant_context), store=Depends(get_store)):
    """服务端认证取 user/tenant，生成不透明 thread_id；sessions 与 scope 同事务（Phase1 内存原子）。"""
    settings = get_settings()
    thread_id = generate_thread_id()
    now = _now()
    store.create_session(ctx.tenant_id, ctx.user_id, thread_id, now, settings.session_ttl_days)
    store.append_audit(ctx.tenant_id, ctx.user_id, "session.create", "session", thread_id,
                       {"thread_id": thread_id}, now)
    return NewSessionResponse(thread_id=thread_id, session_id=thread_id)


@router.get("/sessions", response_model=list[SessionRead])
async def list_sessions(ctx=Depends(get_tenant_context), store=Depends(get_store)):
    # customer 只返回本人会话；staff 返回租户内全部（RBAC）。
    user_id = ctx.user_id if not _staff_role(ctx) else None
    sessions = store.list_sessions(ctx.tenant_id, user_id)
    return [SessionRead(thread_id=s.thread_id, title=s.title, created_at=s.created_at,
                        expires_at=s.expires_at, status=s.status.value, message_count=s.message_count,
                        user_id=s.user_id)
            for s in sessions]


@router.get("/sessions/{thread_id}", response_model=SessionRead)
async def get_session(thread_id: str, ctx=Depends(get_tenant_context), store=Depends(get_store)):
    validate_session_owner(store, ctx, thread_id)
    s = store.get_session(ctx.tenant_id, thread_id)
    return SessionRead(thread_id=s.thread_id, title=s.title, created_at=s.created_at,
                       expires_at=s.expires_at, status=s.status.value, message_count=s.message_count)


@router.get("/sessions/{thread_id}/messages", response_model=SessionMessagesResponse)
async def get_session_messages(thread_id: str, ctx=Depends(get_tenant_context),
                               store=Depends(get_store), runner=Depends(get_runner)):
    validate_session_owner(store, ctx, thread_id)
    config = {"configurable": {"thread_id": thread_id}}
    async with _checkpoint_scope(store, ctx, thread_id, "history"):
        snapshot = await runner.graph.aget_state(config)
    messages = (snapshot.values or {}).get("messages", [])
    return SessionMessagesResponse(messages=[_message_to_dict(m) for m in messages])


# ---- 聊天 AI 流（SSE）----
@router.post("/chat")
async def chat(request: ChatRequest, ctx=Depends(get_tenant_context),
               runner=Depends(get_runner), store=Depends(get_store),
               last_event_id: Optional[str] = Header(default=None, alias="Last-Event-ID")):
    get_metrics().counter("api_requests_total", ("route", "method"),
                          {"route": "chat", "method": request.mode})
    settings = get_settings()
    if request.mode == "resume":
        if not last_event_id:
            raise HTTPException(status_code=422, detail="Last-Event-ID is required")
        try:
            cursor = int(last_event_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Last-Event-ID must be an integer")
        # 重放前校验流归属与会话可用性（跨用户/过期/删除中拒绝并审计）。
        validate_stream_owner(store, ctx, request.stream_id)
        return StreamingResponse(
            runner.run_resume(ctx, request.stream_id, cursor),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    # start：在流开始前做会话归属校验，非法则返回 404（不泄露存在）。
    validate_session_owner(store, ctx, request.thread_id)
    return StreamingResponse(
        runner.run_start(ctx, request.thread_id, request.client_request_id, request.message,
                         settings.session_ttl_days),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---- 审批 ----
@router.get("/approvals", response_model=list[ApprovalRead])
async def list_approvals(ctx=Depends(get_tenant_context), store=Depends(get_store)):
    approvals = store.list_approvals(ctx.tenant_id)
    visible = _visible_thread_ids(store, ctx)
    if visible is not None:
        approvals = [a for a in approvals if a.thread_id in visible]
    return [ApprovalRead(approval_id=a.approval_id, operation_id=a.operation_id,
                         thread_id=a.thread_id,
                         pending_action=a.pending_action.value, status=a.status.value,
                         order_id=a.order_id, amount=a.amount, reason=a.reason,
                         created_at=a.created_at, approver=a.approver, feedback=a.feedback)
            for a in approvals]


@router.get("/approvals/{approval_id}", response_model=ApprovalRead)
async def get_approval(approval_id: str, ctx=Depends(get_tenant_context), store=Depends(get_store)):
    try:
        a = store.get_approval(ctx.tenant_id, approval_id)  # 跨租户 → 404（不泄露存在）
    except DomainError:
        audit_security_denial(store, ctx.tenant_id, ctx.user_id, "approval_access_denied",
                              "approval", approval_id)
        raise
    _check_visible(store, ctx, a.thread_id)
    return ApprovalRead(approval_id=a.approval_id, operation_id=a.operation_id,
                        thread_id=a.thread_id,
                        pending_action=a.pending_action.value, status=a.status.value,
                        order_id=a.order_id, amount=a.amount, reason=a.reason,
                        created_at=a.created_at, approver=a.approver, feedback=a.feedback)


@router.post("/approvals/{approval_id}/decision", response_model=ApprovalDecisionResponse)
async def decide_approval(approval_id: str, request: ApprovalDecisionRequest,
                          ctx=Depends(get_tenant_context), store=Depends(get_store),
                          runner=Depends(get_runner)):
    """审批决定：恢复既有待审批操作，返回权威 operation_id，保持幂等。

    绑定原则：审批决定唯一锚定 approval 记录携带的 operation_id / thread_id / pending_action；
    请求回传的 operation_id / pending_action 若提供则必须一致。并发/超时后再次决策
    不恢复图、不重复执行（CAS 抢占返回 claimed=False）。
    """
    get_metrics().counter("approval_decisions_total", ("route",),
                          {"route": "approval.decision"})
    try:
        require_role(ctx, {"admin", "approver"})
    except DomainError:
        audit_security_denial(store, ctx.tenant_id, ctx.user_id, "forbidden",
                              "approval", approval_id)
        raise
    try:
        approval = store.get_approval(ctx.tenant_id, approval_id)  # 跨租户 → 404
    except DomainError:
        audit_security_denial(store, ctx.tenant_id, ctx.user_id, "approval_access_denied",
                              "approval", approval_id)
        raise
    validate_session_owner(store, ctx, approval.thread_id)

    # 绑定复核：审批决定必须锚定到唯一的 operation + 动作 + 线程。
    operation = store.get_operation(ctx.tenant_id, approval.operation_id)
    if operation.pending_action != approval.pending_action:
        raise DomainError(ErrorCode.APPROVAL_BINDING_MISMATCH,
                          "审批单与操作的动作类型不一致", 422)
    if operation.thread_id != approval.thread_id:
        raise DomainError(ErrorCode.APPROVAL_BINDING_MISMATCH,
                          "审批单与操作的线程不一致", 422)
    if request.operation_id is not None and request.operation_id != approval.operation_id:
        raise DomainError(ErrorCode.APPROVAL_BINDING_MISMATCH,
                          "请求的 operation_id 与审批单绑定不一致", 422)
    if request.pending_action is not None and request.pending_action != approval.pending_action.value:
        raise DomainError(ErrorCode.APPROVAL_BINDING_MISMATCH,
                          "请求的 pending_action 与审批单绑定不一致", 422)

    # 二次确认：批准必须显式勾选确认。
    if request.approved and not request.confirmation:
        raise HTTPException(status_code=422, detail="approval requires confirmation")

    # 已决策 / 超时：幂等重放，返回既有 operation 结果，不重复执行、不恢复图。
    if approval.status != ApprovalStatus.PENDING:
        op = store.get_operation(ctx.tenant_id, approval.operation_id)
        msg = ("审批已超时转人工，返回既有结果。" if approval.status == ApprovalStatus.TIMEOUT
               else "审批已处理，返回既有结果。")
        return ApprovalDecisionResponse(operation_id=op.operation_id, status=op.status.value,
                                        message=msg)

    # CAS 抢占：仅抢占成功者恢复图执行；并发/重复决策者只返回既有结果，不双执行。
    approval, claimed = store.claim_approval_decision(
        ctx.tenant_id, approval_id, ctx.user_id, request.approved, request.feedback, _now())
    if not claimed:
        op = store.get_operation(ctx.tenant_id, approval.operation_id)
        return ApprovalDecisionResponse(operation_id=op.operation_id, status=op.status.value,
                                        message="审批已被处理，返回既有结果，未重复执行。")

    config = {"configurable": {"thread_id": approval.thread_id}}
    op = None
    async with _checkpoint_scope(store, ctx, approval.thread_id, "approval_resume"):
        # 自托管观测：审批恢复+执行包在 Langfuse span（metadata 带 tenant/session，
        # 输入输出经脱敏）。未配置 Langfuse 时 no-op，绝不阻塞审批主链路。
        with trace_span("approval.decide", tenant_id=ctx.tenant_id,
                        session_id=approval.thread_id, user_id=ctx.user_id,
                        input={"approved": request.approved, "approver": ctx.user_id,
                               "feedback": request.feedback,
                               "operation_id": approval.operation_id,
                               "pending_action": approval.pending_action.value},
                        output=lambda: {"operation_id": approval.operation_id,
                                        "status": op.status.value} if op else None):
            await runner.graph.ainvoke(
                Command(resume={"approved": request.approved, "feedback": request.feedback,
                                "approver": ctx.user_id}),
                config=config,
            )
            op = store.get_operation(ctx.tenant_id, approval.operation_id)
    store.append_audit(ctx.tenant_id, ctx.user_id, "approval.decide", "approval", approval_id,
                       {"approved": request.approved, "operation_id": op.operation_id,
                        "status": op.status.value}, _now())
    message = "审批已通过，已执行。" if request.approved else "审批已拒绝，未执行。"
    return ApprovalDecisionResponse(operation_id=op.operation_id, status=op.status.value, message=message)


# ---- 操作状态 ----
@router.get("/operations/{operation_id}", response_model=OperationRead)
async def get_operation(operation_id: str, ctx=Depends(get_tenant_context), store=Depends(get_store)):
    try:
        op = store.get_operation(ctx.tenant_id, operation_id)  # 跨租户 → 404
    except DomainError:
        audit_security_denial(store, ctx.tenant_id, ctx.user_id, "operation_access_denied",
                              "operation", operation_id)
        raise
    _check_visible(store, ctx, op.thread_id)
    return OperationRead(operation_id=op.operation_id, thread_id=op.thread_id, order_id=op.order_id,
                         pending_action=op.pending_action.value, status=op.status.value,
                         idempotency_key=op.idempotency_key, created_at=op.created_at, result=op.result)


# ---- 执行记录（幂等锚点 + 状态机；供人工/对账核对）----
@router.get("/executions/{execution_id}", response_model=ExecutionRead)
async def get_execution(execution_id: str, ctx=Depends(get_tenant_context), store=Depends(get_store)):
    try:
        rec = store.get_execution_record(ctx.tenant_id, execution_id)  # 跨租户 → 404
    except DomainError:
        audit_security_denial(store, ctx.tenant_id, ctx.user_id, "execution_access_denied",
                              "execution", execution_id)
        raise
    op = store.get_operation(ctx.tenant_id, rec.operation_id)
    _check_visible(store, ctx, op.thread_id)
    return ExecutionRead(execution_id=rec.execution_id, operation_id=rec.operation_id,
                         order_id=rec.order_id, pending_action=rec.pending_action.value,
                         idempotency_key=rec.idempotency_key, mode=rec.mode.value,
                         status=rec.status.value, amount=rec.amount,
                         external_txn_id=rec.external_txn_id, created_at=rec.created_at,
                         updated_at=rec.updated_at, confirmed_at=rec.confirmed_at,
                         receipt=rec.receipt, compensation_status=rec.compensation_status,
                         last_error=rec.last_error)


# ---- 资金/业务网关回调（webhook，HMAC 验签 + 非重放窗口）----
def _callback_tenant(raw_body: bytes) -> tuple[str, str]:
    """从回调载荷尽力提取 (tenant_id, execution_id) 供审计；无法解析返回 ("","")。"""
    try:
        payload = json.loads(raw_body.decode("utf-8"))
        return str(payload.get("tenant_id") or ""), str(payload.get("execution_id") or "")
    except Exception:
        return "", ""


@router.post("/callbacks/{channel}")
async def provider_callback(channel: str, request: Request, store=Depends(get_store)):
    """外部资金/业务网关回调端点（无会话/租户鉴权，凭签名 + nonce 重放保护）。

    客户端/租户身份不被信任：以 HMAC 签名为准，执行记录由 execution_id 定位，
    金额一致性校验 + nonce 非重放窗口保证重复回调/重放不产生重复副作用。
    """
    body = await request.body()
    signature = request.headers.get("X-Signature") or request.headers.get("X-Payment-Signature") or ""
    adapter = request.app.state.adapter
    engine = adapter.make_execution_engine(store)
    result = engine.apply_callback(body, signature)
    tenant_id, execution_id = _callback_tenant(body)
    # 审计（验签失败无可信租户，仅从载荷尽力提取；一致成功/冲突均留痕）。
    subject = (result.execution_id or execution_id) or ""
    store.append_audit(tenant_id or "", subject, f"execution.callback.{result.reason}",
                       "execution", result.execution_id or execution_id or "",
                       {"channel": channel, "applied": result.applied, "reason": result.reason}, _now())
    if result.reason == "signature_invalid":
        raise HTTPException(status_code=401, detail="回调签名校验失败")
    if result.reason == "not_found":
        raise HTTPException(status_code=404, detail="执行记录不存在")
    # 重放/冲突/中间态：HTTP 200 且 applied=False（幂等/对账语义），不当作错误。
    return {"applied": result.applied, "reason": result.reason,
            "execution_id": result.execution_id, "status": result.status,
            "receipt": result.receipt}


# ---- 客户订单查询（只读；customer 仅本人订单，staff 租户内全部；均审计拒绝路径）----
@router.get("/orders/{order_id}", response_model=OrderRead)
async def get_order(order_id: str, ctx=Depends(get_tenant_context), store=Depends(get_store)):
    from src.tools import mock_data
    order = mock_data.get_order(ctx.tenant_id, order_id)
    if order is None:
        # 不区分"订单不存在"与"不属于本租户"；统一 404 并审计。
        audit_security_denial(store, ctx.tenant_id, ctx.user_id, "order_access_denied",
                              "order", order_id)
        raise DomainError(ErrorCode.NOT_FOUND, "订单不存在", 404)
    # 同租户跨用户越权防护：customer 仅本人订单；无权查看按"不存在"处理（不泄露存在）。
    if (order.get("user_id") and order["user_id"] != ctx.user_id and not _staff_role(ctx)):
        audit_security_denial(store, ctx.tenant_id, ctx.user_id, "cross_user_order",
                              "order", order_id, {"owner": order["user_id"]})
        raise DomainError(ErrorCode.NOT_FOUND, "订单不存在", 404)
    shipping = mock_data.get_shipping(ctx.tenant_id, order_id)
    return OrderRead(
        order_id=order["order_id"], status=order["status"],
        total_amount=order["total_amount"], items=order["items"],
        carrier=order.get("carrier"), tracking_no=order.get("tracking_no"),
        shipping_events=(shipping["events"] if shipping else []),
    )


# ---- 成员列表（仅 admin；租户成员与服务端角色）----
@router.get("/members", response_model=list[MemberRead])
async def list_members(ctx=Depends(get_tenant_context), store=Depends(get_store)):
    try:
        require_role(ctx, {"admin"})
    except DomainError:
        audit_security_denial(store, ctx.tenant_id, ctx.user_id, "forbidden", "members", "")
        raise
    members = store.list_members(ctx.tenant_id)
    return [MemberRead(tenant_id=m.tenant_id, user_id=m.user_id, role=m.role.value,
                       status=m.status.value) for m in members]


# ---- 受控审计查询（租户作用域；按 租户/会话/审批/operation_id 追溯）----
@router.get("/audit", response_model=list[AuditRead])
async def get_audit(ctx=Depends(get_tenant_context), store=Depends(get_store),
                    target_type: str | None = None, target_id: str | None = None,
                    thread_id: str | None = None, action: str | None = None,
                    user_id: str | None = None, limit: int = 200):
    """租户内审计查询（受控审计/复核用）。返回的 detail 已脱敏。

    权限：staff（admin/approver/agent）可查租户内审计；customer 仅可查本人线索。
    追溯维度：tenant_id（恒为当前登录租户）、thread_id（会话）、target_type=approval
    + target_id=approval_id（审批）、target_type=operation + target_id=operation_id（操作）。
    """
    get_metrics().counter("audit_queries_total", ("route",), {"route": "audit"})
    effective_user = ctx.user_id if not _staff_role(ctx) else (user_id or None)
    records = store.search_audit(ctx.tenant_id, thread_id=thread_id, target_type=target_type,
                                 target_id=target_id, action=action, user_id=effective_user,
                                 limit=limit)
    visible = _visible_thread_ids(store, ctx)
    if visible is not None:
        # customer：仅保留与其会话相关（session 目标 或 detail.thread_id 命中本人会话）。
        def rel(r: AuditRead) -> bool:
            detail_thread = (r.detail.get("thread_id") if isinstance(r.detail, dict) else None)
            return (r.target_type == "session" and r.target_id in visible) or \
                   (detail_thread in visible)
        records = [r for r in records if rel(r)]
    return [AuditRead(audit_id=r.audit_id, tenant_id=r.tenant_id, user_id=r.user_id,
                      action=r.action, target_type=r.target_type, target_id=r.target_id,
                      detail=_redact_detail(r.detail), created_at=r.created_at) for r in records]


# ---- 免认证健康/就绪端点（同源反向代理下供 nginx/健康检查使用）----
# 只报告"进程存活"，不暴露任何租户数据或内部拓扑。探测 /api/healthz。
@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


# ---- Prometheus 指标（自托管监控抓取；仅有界聚合，不含租户/高基数标签）----
# 只暴露聚合计数/直方图（route/status/kind 等有界维度），绝不暴露 tenant_id 等明细，
# 租户明细走受控审计查询。可被自托管 Prometheus 抓取、Grafana 展示。
@router.get("/metrics")
async def metrics() -> str:
    return PlainTextResponse(get_metrics().render(), media_type="text/plain")
