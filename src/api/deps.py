"""FastAPI 依赖注入。"""
from __future__ import annotations

from fastapi import Header, HTTPException, Request

from src.auth.security import audit_security_denial, resolve_tenant_context
from src.core.launch_gate import tenant_allowed_for_launch
from src.core.types import TenantContext


def get_store(request: Request):
    return request.app.state.store


def get_llm(request: Request):
    return request.app.state.llm


def get_runner(request: Request):
    return request.app.state.chat_runner


def get_settings_dep(request: Request):
    return request.app.state.settings


async def get_tenant_context(
    request: Request,
    authorization: str | None = Header(default=None),
) -> TenantContext:
    """服务端解析认证主体 → 租户/成员/角色校验 → 不可变 TenantContext。

    settings 取自 app.state.settings，保证受限环境的 Mock 认证 fail-closed 生效。
    首次上线门控：若配制了上线租户白名单（LAUNCH_ALLOWED_TENANTS），仅名单内租户可访问；
    其它租户拒绝并写审计（全量审计留痕）。
    """
    store = get_store(request)
    ctx = resolve_tenant_context(store, authorization, settings=request.app.state.settings)
    if not tenant_allowed_for_launch(request.app.state.settings, ctx.tenant_id):
        audit_security_denial(store, ctx.tenant_id, ctx.user_id, "launch_tenant_not_allowed",
                              "tenant", ctx.tenant_id)
        raise HTTPException(status_code=403, detail="租户未纳入本次上线白名单。")
    return ctx
