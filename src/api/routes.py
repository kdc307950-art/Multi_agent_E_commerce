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

import ipaddress
import json
import os
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
from src.auth.ratelimit import extract_client_ip, resolve_login_rate_limiter
from src.config import get_settings
from src.core.types import (
    ApprovalStatus,
    DomainError,
    ErrorCode,
    Role,
    SessionStatus,
    generate_thread_id,
)
from src.observability.logging import get_logger
from src.observability.metrics import get_metrics
from src.observability.tracing import trace_span

router = APIRouter()


def _now() -> float:
    return time.time()


# ---- API 请求指标（有界标签 route/method/status；严禁 tenant_id/user_id/order_id 等明细）----
def _api_route_label(request: Request | None) -> str:
    """派生有界 route 标签：取匹配到的路由模板，折叠路径参数为 `param`，去掉 /api 前缀。

    只保留有界路由名（如 chat / approvals/decision / healthz），绝不把 order_id/thread_id
    等请求路径参数当作标签值（禁止高基数与敏感维度）。"""
    route = request.scope.get("route") if request is not None else None
    path = getattr(route, "path", None)
    if not path:
        try:
            path = request.url.path if request is not None else ""
        except Exception:
            path = ""
    if not isinstance(path, str) or not path:
        return "other"
    coarse: list[str] = []
    for seg in [s for s in path.split("/") if s]:
        if seg.startswith("{") and seg.endswith("}"):
            coarse.append("param")
        else:
            coarse.append(seg)
    if coarse and coarse[0] == "api":
        coarse = coarse[1:]
    return "/".join(coarse) or "other"


def record_api_request_metric(request: Request | None, status_code: int) -> None:
    """记录一次 API 请求结果（有界标签 route/method/status，仅 3 位状态码）。

    调用方（HTTP 中间件）在请求出栈（含异常 500）后调用，保证 status 真实。"""
    try:
        method = request.method if request is not None else "GET"
    except Exception:
        method = "GET"
    get_metrics().counter("api_requests_total", ("route", "method", "status"),
                          {"route": _api_route_label(request), "method": method,
                           "status": str(int(status_code))})


# 指标抓取来源判定（/api/metrics 内网化，见 deploy/EGRESS_POLICY.md）。
# 非受限环境默认“仅内网”回退网段：回环 + RFC1918 私网（完全自托管红线，绝不默认放公网）。
_DEFAULT_METRICS_INTERNAL_CIDRS = ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")

# 平台级安全日志通道：回调端点「不可信/未知」情形只写 security logger（结构化日志，
# 见 observability/logging.py 的 get_logger + PIIRedactionFilter + JsonFormatter），
# 走审计/日志平台（Loki 等），**绝不**落 Prometheus 标签（高基数/明细不入 /api/metrics）。
_security_log = get_logger("security")

# 「不可信/未知」回调结果：未定位到可信执行记录（或身份不可信）。这些情形只写平台级
# 安全日志，**绝不**用请求 body 中不可信 tenant_id 写租户审计、**也**不写数据库 audit。
# 其中 not_found 既含「执行记录不存在」也含「跨租户伪造/越权定位」——统一按未知处理，
# 不给攻击者区分存在性的反馈，且不污染审计链路。
_UNTRUSTED_CALLBACK_REASONS = frozenset({
    "signature_invalid",   # 验签失败（HMAC 校验未通过）
    "bad_payload",         # 非法 JSON / 不可解析的 body
    "missing_identity",    # body 缺 tenant_id / execution_id（身份不可信）
    "missing_nonce",       # 缺 nonce（重放防护字段缺失）
    "not_found",           # 未知 execution_id（含跨租户伪造，未定位到可信执行记录）
})


def _ip_in_network(ip_str: str, network: str) -> bool:
    """判断 ip_str 是否落在 network（单个 IP 或 CIDR）内；非法输入一律 False。"""
    if not ip_str:
        return False
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    try:
        return addr in ipaddress.ip_network(network, strict=False)
    except ValueError:
        return False


def _metrics_request_allowed(client_ip: str, settings) -> bool:
    """/api/metrics 来源限制：仅白名单内网段可抓取，否则 fail-closed 拒绝。

    规则（受限环境 preview/production 恒为仅内网，绝不因配置遗漏暴露公网）：
    - `metrics_expose_internal_only=true`：
        * 已配置 `metrics_allowed_sources`（IP/CIDR 列表）：命中即允许；
        * 未配置白名单：受限环境拒绝（fail-closed）；非受限环境回退“仅回环 + RFC1918 私网”。
    - `metrics_expose_internal_only=false`（显式关闭门控）：仅非受限环境放行；
      受限环境即使关闭门控也拒绝（杜绝误配把聚合指标暴露公网）。
    """
    if settings.metrics_expose_internal_only:
        allowed = settings.metrics_allowed_source_list
        if allowed:
            return any(_ip_in_network(client_ip, c) for c in allowed)
        if settings.is_restricted_env:
            return False
        return any(_ip_in_network(client_ip, c) for c in _DEFAULT_METRICS_INTERNAL_CIDRS)
    # 显式关闭门控：仅非受限环境放行（受限环境 fail-closed）。
    return not settings.is_restricted_env


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
async def login(body: LoginRequest, request: Request, store=Depends(get_store),
                settings=Depends(get_settings_dep)):
    """登录端点：双维度限流 + 失败退避 + 服务端凭据校验 → 签发真实 JWT。

    仅 `auth_backend=real` + 已配置 `AUTH_LOGIN_CREDENTIALS` 可用；否则 fail-closed（503）。
    限流/退避拒绝路径写脱敏审计（reason: login_rate_limited / ip_rate_limited /
    login_backoff / redis_unavailable）并记录 Prometheus 指标；成功登录也留痕（不含凭据）。
    """
    client_ip = extract_client_ip(request, settings.login_trusted_proxy_depth)
    limiter = resolve_login_rate_limiter(settings, request.app.state)
    metrics = get_metrics()
    account = f"{body.tenant_id}:{body.user_id}"

    # 1) 预检查限流（不消耗配额）：账号/IP 超限或退避期内 → 拒绝 + 审计 + 指标。
    decision = limiter.check(body.tenant_id, body.user_id, client_ip)
    if not decision.allowed:
        reason = decision.reason or "login_rate_limited"
        metrics.counter("login_rate_limited_total", ("reason",), {"reason": reason})
        metrics.counter("login_attempts_total", ("result",), {"result": "rate_limited"})
        audit_security_denial(store, body.tenant_id, body.user_id, reason, "auth", account,
                              {"client_ip": client_ip,
                               "retry_after_s": round(decision.retry_after, 3)})
        raise DomainError(
            ErrorCode.TOO_MANY_REQUESTS, "登录尝试过于频繁，请稍后再试", 429,
            {"reason": reason, "client_ip": client_ip,
             "retry_after_s": round(decision.retry_after, 3)})

    # 2) 实际服务端凭据校验（失败路径在 issue_login_token 内写脱敏审计）。
    try:
        token = issue_login_token(store, settings, body.tenant_id, body.user_id, body.credential)
    except DomainError:
        # 凭据错/账号不存在/成员被拒 → 更新失败计数与退避（防暴力登录核心）。
        limiter.record(body.tenant_id, body.user_id, client_ip, ok=False)
        metrics.counter("login_attempts_total", ("result",), {"result": "failure"})
        raise

    # 3) 成功：重置该账号失败计数与退避；保留成功审计，不携带凭据。
    limiter.record(body.tenant_id, body.user_id, client_ip, ok=True)
    metrics.counter("login_attempts_total", ("result",), {"result": "success"})
    store.append_audit(body.tenant_id, body.user_id, "auth.login", "auth", account,
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
    # API 请求计数由 HTTP 中间件统一记录 api_requests_total{route,method,status}
    # （含最终 status），此处不再重复计数，避免同一指标出现两种标签集。
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
    _decision_started = _now()

    def _record_approval_decision(status: str) -> None:
        """记录一次审批决策（有界标签: route/status）。status ∈ approved|rejected|timeout|replay|error|forbidden。"""
        get_metrics().counter("approval_decisions_total", ("route", "status"),
                              {"route": "approval.decision", "status": status})

    def _record_approval_latency() -> None:
        """记录审批决策延迟（秒）到直方图（有界: route 标签）。"""
        get_metrics().observe("approval_decision_latency_seconds", _now() - _decision_started,
                              ("route",), {"route": "approval.decision"})

    try:
        require_role(ctx, {"admin", "approver"})
    except DomainError:
        _record_approval_decision("forbidden")
        audit_security_denial(store, ctx.tenant_id, ctx.user_id, "forbidden",
                              "approval", approval_id)
        raise
    try:
        approval = store.get_approval(ctx.tenant_id, approval_id)  # 跨租户 → 404
    except DomainError:
        _record_approval_decision("error")
        audit_security_denial(store, ctx.tenant_id, ctx.user_id, "approval_access_denied",
                              "approval", approval_id)
        raise
    validate_session_owner(store, ctx, approval.thread_id)

    # 绑定复核：审批决定必须锚定到唯一的 operation + 动作 + 线程。
    operation = store.get_operation(ctx.tenant_id, approval.operation_id)
    if operation.pending_action != approval.pending_action:
        _record_approval_decision("error")
        raise DomainError(ErrorCode.APPROVAL_BINDING_MISMATCH,
                          "审批单与操作的动作类型不一致", 422)
    if operation.thread_id != approval.thread_id:
        _record_approval_decision("error")
        raise DomainError(ErrorCode.APPROVAL_BINDING_MISMATCH,
                          "审批单与操作的线程不一致", 422)
    if request.operation_id is not None and request.operation_id != approval.operation_id:
        _record_approval_decision("error")
        raise DomainError(ErrorCode.APPROVAL_BINDING_MISMATCH,
                          "请求的 operation_id 与审批单绑定不一致", 422)
    if request.pending_action is not None and request.pending_action != approval.pending_action.value:
        _record_approval_decision("error")
        raise DomainError(ErrorCode.APPROVAL_BINDING_MISMATCH,
                          "请求的 pending_action 与审批单绑定不一致", 422)

    # 二次确认：批准必须显式勾选确认。
    if request.approved and not request.confirmation:
        _record_approval_decision("error")
        raise HTTPException(status_code=422, detail="approval requires confirmation")

    # 已决策 / 超时：幂等重放，返回既有 operation 结果，不重复执行、不恢复图。
    if approval.status != ApprovalStatus.PENDING:
        op = store.get_operation(ctx.tenant_id, approval.operation_id)
        if approval.status == ApprovalStatus.TIMEOUT:
            _record_approval_decision("timeout")
            # 审批超时 → 人工介入（kind=approval_timeout），作为有界人工介入计数。
            get_metrics().counter("human_intervention_total", ("kind",),
                                  {"kind": "approval_timeout"})
            msg = "审批已超时转人工，返回既有结果。"
        else:
            _record_approval_decision("replay")
            msg = "审批已处理，返回既有结果。"
        return ApprovalDecisionResponse(operation_id=op.operation_id, status=op.status.value,
                                        message=msg)

    # CAS 抢占：仅抢占成功者恢复图执行；并发/重复决策者只返回既有结果，不双执行。
    approval, claimed = store.claim_approval_decision(
        ctx.tenant_id, approval_id, ctx.user_id, request.approved, request.feedback, _now())
    if not claimed:
        _record_approval_decision("replay")
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
    # 决策完成：记录 status + 延迟（仅对真正执行恢复的决策度量，避免早期校验计入延迟）。
    _record_approval_decision("approved" if request.approved else "rejected")
    _record_approval_latency()
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
@router.post("/callbacks/{channel}")
async def provider_callback(channel: str, request: Request, store=Depends(get_store),
                            settings=Depends(get_settings_dep)):
    """外部资金/业务网关回调端点（无会话/租户鉴权，凭签名 + nonce 重放保护）。

    客户端/租户身份不被信任：以 HMAC 签名为准，执行记录由 execution_id 定位，
    金额一致性校验 + nonce 非重放窗口保证重复回调/重放不产生重复副作用。

    安全审计边界（见《Agent 宪法》「绝不信任客户端身份」）：
    - 「不可信/未知」情形（验签失败 / 非法 JSON / 缺失身份或 nonce / 未知 execution_id）：
      只写**平台级安全日志**（`get_logger("security")`，字段 channel/client_ip/reason/
      execution_id_present/provider），**绝不**用请求 body 的不可信 tenant_id 写租户审计，
      也**不写数据库 audit**——防止攻击者伪造他人/不存在租户的审计污染审计链路。
    - 「签名验签通过且定位到可信执行记录」的情形（confirmed / failed_dispatch / replay /
      amount_mismatch / illegal_transition / processing / terminal_locked）：租户审计已在
      `store.apply_callback_atomic` **同一事务**内用**执行记录的可信 tenant_id** 写入
      （user_id="callback"），本端点只返回 HTTP 响应，不再用 body tenant_id 追加审计。
    """
    body = await request.body()
    signature = request.headers.get("X-Signature") or request.headers.get("X-Payment-Signature") or ""
    adapter = request.app.state.adapter
    engine = adapter.make_execution_engine(store)
    result = engine.apply_callback(body, signature)

    # 回调结果计数（有界标签: reason；reason 来自固定集合，绝不引入执行记录/租户明细）。
    get_metrics().counter("execution_callback_total", ("reason",), {"reason": result.reason})

    # 「不可信/未知」→ 平台级安全日志（仅结构化日志通道，不入 Prometheus 标签 / 无 DB audit）。
    if result.reason in _UNTRUSTED_CALLBACK_REASONS:
        # 不可信回调 → 安全拒绝计数（有界 kind=reason；reason ∈ 固定集合），
        # 与「审计拒绝不入指标」的原则并行：指标只聚合有界维度，明细仍走安全日志。
        get_metrics().counter("security_denials_total", ("kind",), {"kind": result.reason})
        client_ip = extract_client_ip(request, settings.login_trusted_proxy_depth)
        _security_log.warning(
            "payment-callback rejected (untrusted)",
            extra={
                "channel": channel,
                "client_ip": client_ip,
                "reason": result.reason,
                # 仅标记「请求体是否解析到 execution_id」这一事实，绝不作为授权/审计主体。
                "execution_id_present": bool(result.execution_id),
                "provider": getattr(adapter, "provider_name", None) or "unknown",
            },
        )

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
async def get_order(order_id: str, request: Request,
                    ctx=Depends(get_tenant_context), store=Depends(get_store)):
    # 受控真实读路径：一律经 EcommerceAdapter（底层为受控数据源，见 business_data_backend），
    # 服务端注入 tenant_id 并在适配器内校验资源归属；禁止直接读数据源/mock_data。
    from src.tools import AdapterError
    adapter = request.app.state.adapter
    try:
        order = adapter.get_order(ctx.tenant_id, ctx.user_id, ctx.role.value, order_id)
        shipping = adapter.get_shipping(ctx.tenant_id, ctx.user_id, ctx.role.value, order_id)
    except AdapterError as exc:
        # 对外统一 404（不区分"订单不存在"与"不属于本租户"，避免泄露存在性）。
        # 审计动作区分：同租户归属拒绝（order_not_owned）记录 cross_user_order 并带 owner；
        # 其余（不存在/跨租户）记录统一 order_access_denied；均 fail-closed 拒绝。
        # 订单查询拒绝 → 人工介入计数（kind=order_deny，有界标签，杜绝高基数）。
        get_metrics().counter("human_intervention_total", ("kind",), {"kind": "order_deny"})
        if exc.code == "order_not_owned":
            audit_security_denial(store, ctx.tenant_id, ctx.user_id, "cross_user_order",
                                  "order", order_id, {"owner": exc.owner})
        else:
            audit_security_denial(store, ctx.tenant_id, ctx.user_id, "order_access_denied",
                                  "order", order_id, {"reason": exc.code})
        raise DomainError(ErrorCode.NOT_FOUND, "订单不存在", 404)
    return OrderRead(
        order_id=order.order_id, status=order.status,
        total_amount=order.total_amount, items=order.items,
        carrier=order.carrier, tracking_no=order.tracking_no,
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
# 内网化：来源 IP 命中 metrics_allowed_sources 才返回，否则 403（fail-closed）。
# 来源判定沿用与登录一致的 login_trusted_proxy_depth 可信代理深度。


def _load_drill_gauges() -> None:
    """把灾备演练脚本写出的 RPO/RTO 状态文件合并到指标 registry。

    这是 alert-rules.yml 中 `RpoExceeded`/`RtoExceeded` 的**写路径**：DR 脚本
    （deploy/scripts/restore_drill.sh / backup_encrypted.sh，经 drill_metric_exporter.py）把实测
    RPO/RTO 写入状态文件（DRILL_GAUGE_STATE，host 路径经 compose 以只读挂载进 api 容器）；
    Prometheus 经 `api:8000/api/metrics` 抓取时即可采集到
    `drill_rpo_seconds{component="pg_backup"}` / `drill_rto_seconds{component="pg_backup"}`。
    状态文件缺失/非法即忽略（增量、非关键路径；绝不因此阻断 /api/metrics）。
    标签只用有界维度 `component`，绝不接受 tenant_id 等高基数/敏感维度（由 metrics._check_labels 拒绝）。
    """
    state = os.environ.get("DRILL_GAUGE_STATE", "/app/evidence/dr_metrics/drill_gauges.json")
    try:
        with open(state, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return  # 状态文件尚不存在（演练未跑），gauge 缺省即不暴露，规则不误报。
    comp = data.get("component", "pg_backup")
    m = get_metrics()
    if data.get("rpo_seconds") is not None:
        m.set("drill_rpo_seconds", float(data["rpo_seconds"]), ("component",), {"component": comp})
    if data.get("rto_seconds") is not None:
        m.set("drill_rto_seconds", float(data["rto_seconds"]), ("component",), {"component": comp})


@router.get("/metrics")
async def metrics(request: Request, settings=Depends(get_settings_dep)) -> str:
    client_ip = extract_client_ip(request, settings.login_trusted_proxy_depth)
    if not _metrics_request_allowed(client_ip, settings):
        raise HTTPException(status_code=403, detail="metrics unavailable")
    _load_drill_gauges()  # t8：合并灾备 RPO/RTO gauge（闭合 RpoExceeded/RtoExceeded 写路径）
    return PlainTextResponse(get_metrics().render(), media_type="text/plain")
