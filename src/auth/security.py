"""认证与会话归属。

安全红线：
- `tenant_id` / `user_id` 只来自服务端认证后的 `TenantContext`；客户端 body/query/Header
  /thread_id 都不能覆盖。解析失败、租户停用、成员撤销或角色非法一律拒绝。
- `platform_admin` 是独立平台级能力，不作为租户成员角色；它不能调用 /api/chat、
  /api/approvals/{approval_id}/decision 或 execute_* 路径。
- 所有安全拒绝路径都必须写审计（`audit_security_denial`），可回溯到主体/资源与原因。

认证后端（可插拔）：
- `auth_backend=mock`：`mock:<tenant_id>:<user_id>:<role>:<entropy>`，**仅允许**在
  development/test 环境使用；preview/production 下 mock 认证一律 fail-closed（拒绝全部请求）。
- `auth_backend=real`：基于 HS256 JWT（标准库实现）的**真实认证接口**；`auth_jwt_secret`
  缺失或配置不合法时同样 fail-closed。生产必须配置 real 后端并校验成员关系后才能放行。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Protocol

from src.config import get_settings
from src.core.types import (
    DomainError,
    ErrorCode,
    Role,
    Session,
    SessionStatus,
    TenantContext,
)

TOKEN_PREFIX = "mock"

# 脱敏黑名单：凡 detail 键名包含这些子串（不区分大小写），其值一律写 [REDACTED]，
# 保证审计日志永不记录口令、令牌、地址、完整支付凭证等敏感信息。
_SENSITIVE_KEYS = (
    "password", "credential", "secret", "token", "authorization",
    "address", "id_number", "card", "payment", "phone", "email",
)


def _redact_detail(detail: dict | None) -> dict:
    """递归过滤审计 detail 中的敏感键，值是 [REDACTED]。"""
    if not isinstance(detail, dict):
        return {}
    out: dict = {}
    for k, v in detail.items():
        if any(s in str(k).lower() for s in _SENSITIVE_KEYS):
            out[k] = "[REDACTED]"
        elif isinstance(v, dict):
            out[k] = _redact_detail(v)
        elif isinstance(v, list):
            out[k] = [_redact_detail(i) if isinstance(i, dict) else i for i in v]
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# 安全拒绝审计（每个拒绝路径都留痕，且强制脱敏）
# ---------------------------------------------------------------------------
def audit_security_denial(store, tenant_id: str | None, user_id: str | None,
                          reason: str, target_type: str = "auth", target_id: str = "",
                          detail: dict | None = None) -> None:
    """记录一条安全拒绝审计事件；尽力而为，不因审计失败阻断主流程。

    `reason` 为机器可读原因（如 cross_tenant / cross_user / session_expired /
    mock_auth_in_restricted_env / tenant_suspended / role_mismatch / login_failed ...）。

    脱敏保证：detail 中的敏感键（password/credential/token/secret/address/payment/...）
    一律写 [REDACTED]；超长 target_id 截断，避免把令牌或完整凭据写入日志。
    """
    if store is None:
        return
    cleaned = _redact_detail(detail or {})
    try:
        store.append_audit(
            tenant_id or "", user_id or "",
            f"security.deny.{reason}", target_type, _safe_target_id(target_id),
            {"reason": reason, **cleaned}, time.time(),
        )
    except Exception:  # 审计失败不应让一次合规拒绝变成 500
        pass


def _safe_target_id(target_id: str) -> str:
    """对审计 target_id 做长值截断；调用方若把令牌原文传入会被去掉。”

    只有长度 <= 64 才原样保留（资源 id/租户:用户 等）。
    """
    if not target_id:
        return target_id
    if len(target_id) > 64:
        return target_id[:32] + "..."
    return target_id


def token_fingerprint(token: str) -> str:
    """令牌脱敏指纹：只保留 sha256 前 16 位，绝不在日志中出现令牌原文。"""
    return f"token:{hashlib.sha256(token.encode('utf-8')).hexdigest()[:16]}"


# ---------------------------------------------------------------------------
# 认证后端（可插拔）
# ---------------------------------------------------------------------------
class TenantAuthenticator(Protocol):
    """服务端认证接口：把 `Bearer` 令牌解析为 (tenant_id, user_id, role)。

    实现必须只依赖服务端可验证的信息，绝不信任客户端声明的租户/用户。
    """

    def authenticate(self, token: str) -> tuple[str, str, str]:  # pragma: no cover
        ...


class MockTokenAuthenticator:
    """开发/单元测试专用 Mock 认证：解析 `mock:<tenant>:<user>:<role>:<entropy>`。

    仅在 development/test 环境由 `resolve_tenant_context` 允许启用；受限制环境走 fail-closed。
    """

    def authenticate(self, token: str) -> tuple[str, str, str]:
        return parse_token(token)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _derive_kid(secret: str) -> str:
    """由密钥内容确定性派生一个稳定的 kid（轮换/校验一致）。"""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


class JwtAuthenticator:
    """真实认证接口（HS256 JWT，标准库实现），支持密钥轮换。

    生产配置 `auth_backend=real` + `auth_jwt_secret` 后用于校验真实登录态。
    JWT 声明要求：`sub`=user_id、`tenant_id`、`role`（租户成员角色）、`exp/iss/aud` 固定。
    密钥轮换：JWT header 携带 `kid`，校验时按 `kid` 从密钥池选取对应密钥；`rotated_secrets`
    为上一代密钥（仅校验不签发）。未知 `kid`、签名失败、缺失/过期 `exp`、`iss`/`aud` 不匹配
    一律 fail-closed（拒绝），绝不降级到 mock。
    """

    def __init__(self, secret: str, issuer: str = "", audience: str = "",
                 rotated_secrets: list[str] | None = None, default_kid: str = "") -> None:
        if not secret:
            raise DomainError(ErrorCode.AUTH_BACKEND_DISABLED,
                              "未配置真实认证密钥（AUTH_JWT_SECRET），拒绝所有请求", 503)
        active_kid = default_kid or _derive_kid(secret)
        self._secrets: dict[str, str] = {active_kid: secret}
        self._default_kid = active_kid
        for old in (rotated_secrets or []):
            old = old.strip()
            if old:
                self._secrets.setdefault(_derive_kid(old), old)
        self.issuer = issuer
        self.audience = audience

    def _select_secret(self, header: dict) -> str:
        kid = header.get("kid")
        if kid:
            secret = self._secrets.get(str(kid))
            if secret is None:
                raise DomainError(ErrorCode.UNAUTHORIZED, "未知的密钥标识", 401)
            return secret
        # 向后兼容：无 kid 的令牌用当前（active）密钥。
        return self._secrets[self._default_kid]

    def authenticate(self, token: str) -> tuple[str, str, str]:
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token):
            raise DomainError(ErrorCode.UNAUTHORIZED, "无效的认证令牌", 401)
        header_b64, payload_b64, signature_b64 = token.split(".")

        try:
            header = json.loads(_b64url_decode(header_b64))
            payload = json.loads(_b64url_decode(payload_b64))
        except Exception:
            raise DomainError(ErrorCode.UNAUTHORIZED, "无效的认证令牌", 401)

        if header.get("alg") != "HS256":
            raise DomainError(ErrorCode.UNAUTHORIZED, "不支持的签名算法", 401)

        secret = self._select_secret(header)
        expected = hmac.new(secret.encode("utf-8"),
                            f"{header_b64}.{payload_b64}".encode("utf-8"), hashlib.sha256).digest()
        actual = _b64url_decode(signature_b64)
        if not hmac.compare_digest(expected, actual):
            raise DomainError(ErrorCode.UNAUTHORIZED, "签名校验失败", 401)

        # 固定过期时间：必须携带 exp 且未过期（强制）。iss/aud 配置了则必须严格一致。
        exp = payload.get("exp")
        if exp is None:
            raise DomainError(ErrorCode.UNAUTHORIZED, "令牌缺少过期时间", 401)
        if time.time() > float(exp):
            raise DomainError(ErrorCode.UNAUTHORIZED, "令牌已过期", 401)
        if self.issuer and payload.get("iss") != self.issuer:
            raise DomainError(ErrorCode.UNAUTHORIZED, "签发方不匹配", 401)
        if self.audience and payload.get("aud") != self.audience:
            raise DomainError(ErrorCode.UNAUTHORIZED, "受众不匹配", 401)

        tenant_id = payload.get("tenant_id")
        user_id = payload.get("sub")
        role = payload.get("role")
        if not tenant_id or not user_id or role not in Role.tenant_roles():
            raise DomainError(ErrorCode.FORBIDDEN, "令牌缺少合法租户/用户/角色", 403)
        return tenant_id, user_id, role


def get_authenticator(settings) -> TenantAuthenticator:
    """按 settings.authentication 选择认证器（含密钥轮换池）。"""
    if settings.auth_backend == "real":
        rotated = [s for s in (settings.auth_jwt_rotated_secrets or "").split(",") if s.strip()]
        return JwtAuthenticator(secret=settings.auth_jwt_secret,
                                issuer=settings.auth_jwt_issuer,
                                audience=settings.auth_jwt_audience,
                                rotated_secrets=rotated,
                                default_kid=settings.auth_jwt_kid)
    return MockTokenAuthenticator()


def issue_jwt(tenant_id: str, user_id: str, role: Role, settings, now: float | None = None,
              exp: float | None = None) -> str:
    """服务端**签发**真实 HS256 JWT（仅 real 后端可用；缺密钥 fail-closed）。

    固定声明：`iss`/`aud` 取自配置且签发必带；`exp = (now) + auth_jwt_ttl_seconds`；
    `sub`=user_id、`tenant_id`、`role`、`iat`、`jti`（防重放噪声）。header 带 `kid`。
    `now`/`exp` 仅供测试校验过期/轮换场景；缺省使用真实时钟与固定 TTL。
    """
    if settings.auth_backend != "real":
        raise DomainError(ErrorCode.AUTH_BACKEND_DISABLED,
                          "仅 real 认证后端允许签发 JWT（mock 环境请用 issue_token）", 503)
    if not settings.auth_jwt_secret:
        raise DomainError(ErrorCode.AUTH_BACKEND_DISABLED,
                          "未配置真实认证密钥（AUTH_JWT_SECRET），无法签发", 503)
    now = now or time.time()
    exp = exp if exp is not None else now + settings.auth_jwt_ttl_seconds
    kid = settings.auth_jwt_kid or _derive_kid(settings.auth_jwt_secret)
    header = {"alg": "HS256", "typ": "JWT", "kid": kid}
    payload = {
        "sub": user_id, "tenant_id": tenant_id, "role": role.value,
        "iss": settings.auth_jwt_issuer, "aud": settings.auth_jwt_audience,
        "iat": int(now), "exp": int(exp), "jti": secrets.token_hex(8),
    }
    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(settings.auth_jwt_secret.encode("utf-8"),
                   f"{header_b64}.{payload_b64}".encode("utf-8"), hashlib.sha256).digest()
    return f"{header_b64}.{payload_b64}.{_b64url_encode(sig)}"


def parse_login_credentials(settings) -> dict[str, str]:
    """解析 `AUTH_LOGIN_CREDENTIALS` JSON 为 `{<tenant_id>:<user_id>: sha256_hex}` 映射。

    非法格式/空表返回 `{}`（登录随后 fail-closed）。凭据仅存哈希且永不入日志。
    """
    raw = (settings.auth_login_credentials or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def issue_login_token(store, settings, tenant_id: str, user_id: str, credential: str) -> str:
    """登录端点：校验成员关系 + 服务端凭据 → 签发真实 JWT。

    安全红线：仅 real 后端可登录；凭据只在服务端做 sha256 常量时间比对；任何失败均拒绝并
    写脱敏审计（绝不记录 credential 原文）。未配置凭据表 → fail-closed（503）。
    """
    if settings.auth_backend != "real":
        audit_security_denial(store, tenant_id, user_id, "login_backend_not_real", "auth",
                              f"{tenant_id}:{user_id}")
        raise DomainError(ErrorCode.AUTH_BACKEND_DISABLED,
                          "登录服务仅在 real 认证后端启用", 503)

    creds = parse_login_credentials(settings)
    if not creds:
        audit_security_denial(store, tenant_id, user_id, "login_unavailable", "auth",
                              f"{tenant_id}:{user_id}")
        raise DomainError(ErrorCode.AUTH_BACKEND_DISABLED,
                          "未配置登录凭据（AUTH_LOGIN_CREDENTIALS），登录不可用", 503)

    # 先校验租户 active + 成员 active（与其它入口一致，失败不区分具体原因避免泄露）。
    try:
        membership = store.require_active_membership(tenant_id, user_id)
    except DomainError as exc:
        audit_security_denial(store, tenant_id, user_id, "login_membership_rejected",
                              "auth", f"{tenant_id}:{user_id}", {"reason": exc.code})
        raise DomainError(ErrorCode.FORBIDDEN, "登录失败", 403)

    stored = creds.get(f"{tenant_id}:{user_id}")
    if stored is None:
        audit_security_denial(store, tenant_id, user_id, "login_credentials_unknown",
                              "auth", f"{tenant_id}:{user_id}")
        raise DomainError(ErrorCode.UNAUTHORIZED, "登录失败", 401)
    presented = hashlib.sha256((credential or "").encode("utf-8")).hexdigest()
    if not hmac.compare_digest(presented, str(stored)):
        audit_security_denial(store, tenant_id, user_id, "login_failed", "auth",
                              f"{tenant_id}:{user_id}")
        raise DomainError(ErrorCode.UNAUTHORIZED, "登录失败", 401)

    return issue_jwt(tenant_id, user_id, membership.role, settings)


def issue_token(tenant_id: str, user_id: str, role: Role) -> str:
    """仅用于开发/单元测试签发 Mock 令牌；不用于生产认证。

    令牌本身不携带任何关于 lease 的授权信息，鉴权仍由服务端从 Store 校验租户/成员状态。
    """
    entropy = secrets.token_hex(8)
    return f"{TOKEN_PREFIX}:{tenant_id}:{user_id}:{role.value}:{entropy}"


def parse_token(token: str) -> tuple[str, str, str]:
    """解析 Mock 令牌为 (tenant_id, user_id, role)。非法结构抛 UNAUTHORIZED/FORBIDDEN。"""
    parts = token.split(":")
    if len(parts) != 5 or parts[0] != TOKEN_PREFIX:
        raise DomainError(ErrorCode.UNAUTHORIZED, "无效的认证令牌", 401)
    tenant_id, user_id, role, entropy = parts[1], parts[2], parts[3], parts[4]
    if role not in Role.tenant_roles():
        raise DomainError(ErrorCode.FORBIDDEN, "非法租户角色", 403)
    return tenant_id, user_id, role


def _best_effort_token_identity(token: str, settings) -> tuple[str, str]:
    """从令牌尽力提取 (tenant_id, user_id) 供审计；无法解析则返回空串（匿名）。"""
    if settings is not None and settings.auth_backend == "real":
        return "", ""
    try:
        tenant_id, user_id, _ = parse_token(token)
        return tenant_id, user_id
    except Exception:
        return "", ""


def resolve_tenant_context(store, authorization: str | None,
                           settings=None) -> TenantContext:
    """服务端解析认证主体 → 校验租户/成员/角色 → 生成不可变 TenantContext。

    生产/预发布禁止 Mock 认证：`settings.is_restricted_env` 且非 real 后端即 fail-closed。
    """
    settings = settings or get_settings()

    # 受限制环境 fail-closed：禁止 Mock 认证（无论是否自带 Bearer）。
    if settings.is_restricted_env and settings.auth_backend != "real":
        audit_security_denial(store, "", "", "mock_auth_in_restricted_env")
        raise DomainError(ErrorCode.AUTH_BACKEND_DISABLED,
                          "生产/预发布环境禁止使用 Mock 认证，请配置真实认证后端", 503)

    if not authorization or not authorization.startswith("Bearer "):
        audit_security_denial(store, "", "", "missing_token")
        raise DomainError(ErrorCode.UNAUTHORIZED, "缺少认证令牌", 401)
    token = authorization[len("Bearer "):].strip()

    authenticator = get_authenticator(settings)
    try:
        tenant_id, user_id, role_value = authenticator.authenticate(token)
    except DomainError as exc:
        attempt_tenant, attempt_user = _best_effort_token_identity(token, settings)
        audit_security_denial(store, attempt_tenant, attempt_user, exc.code,
                              "auth", token_fingerprint(token), {"message": exc.message})
        raise

    # 校验租户 active + 成员 active + 成员角色一致。
    try:
        membership = store.require_active_membership(tenant_id, user_id)
    except DomainError as exc:
        audit_security_denial(store, tenant_id, user_id, exc.code,
                              "membership", f"{tenant_id}:{user_id}", {"message": exc.message})
        raise
    if membership.role.value != role_value:
        audit_security_denial(store, tenant_id, user_id, "role_mismatch",
                              "membership", f"{tenant_id}:{user_id}")
        raise DomainError(ErrorCode.FORBIDDEN, "令牌角色与成员关系不一致", 403)
    return TenantContext(tenant_id=tenant_id, user_id=user_id, role=membership.role)


def require_role(ctx: TenantContext, allowed: set[str]) -> None:
    """审批等受限入口要求当前角色在允许集中。platform_admin 不是租户角色，不在 allowed。"""
    if ctx.role.value not in allowed:
        raise DomainError(ErrorCode.FORBIDDEN, "当前角色无权执行此操作", 403)


def validate_session_owner(store, ctx: TenantContext, thread_id: str) -> Session:
    """校验租户/用户归属与会话可用性。customer 仅能访问本人会话；agent/admin/approver 按租户 RBAC。

    安全拒绝路径：
    - 会话不存在 → 404 且不泄露存在（审计 session_not_found）。
    - 客户跨用户访问 → 403（审计 cross_user_session）。
    - 会话已过期 / 删除中 → 拒绝（审计 session_expired / session_deleting）。
    """
    session = store.get_session_or_none(ctx.tenant_id, thread_id)
    if session is None:
        audit_security_denial(store, ctx.tenant_id, ctx.user_id, "session_not_found",
                              "session", thread_id)
        raise DomainError(ErrorCode.NOT_FOUND, "会话不存在", 404)

    if session.user_id != ctx.user_id and ctx.role.value not in {"agent", "admin", "approver"}:
        audit_security_denial(store, ctx.tenant_id, ctx.user_id, "cross_user_session",
                              "session", thread_id, {"owner": session.user_id})
        raise DomainError(ErrorCode.FORBIDDEN, "无权访问该会话", 403)

    if session.status == SessionStatus.DELETING:
        audit_security_denial(store, ctx.tenant_id, ctx.user_id, "session_deleting",
                              "session", thread_id)
        raise DomainError(ErrorCode.SESSION_DELETING, "会话删除中，禁止访问", 403)
    if session.status == SessionStatus.EXPIRED or session.expires_at < time.time():
        audit_security_denial(store, ctx.tenant_id, ctx.user_id, "session_expired",
                              "session", thread_id)
        raise DomainError(ErrorCode.SESSION_EXPIRED, "会话已过期", 410)

    return session


def validate_stream_owner(store, ctx: TenantContext, stream_id: str) -> dict:
    """校验 SSE 流的租户/用户归属与会话可用性（resume / 重放路径）。

    - 流不存在 → 404（审计 stream_not_found）。
    - 客户跨用户访问流 → 403（审计 cross_user_stream）。
    - 流所属会话已过期/删除中 → 拒绝（审计 session_expired / session_deleting）。
    """
    try:
        stream = store.get_stream(ctx.tenant_id, stream_id)  # 跨租户 → 404
    except DomainError:
        audit_security_denial(store, ctx.tenant_id, ctx.user_id, "stream_not_found",
                              "stream", stream_id)
        raise
    if stream.get("user_id") != ctx.user_id and ctx.role.value not in {"agent", "admin", "approver"}:
        audit_security_denial(store, ctx.tenant_id, ctx.user_id, "cross_user_stream",
                              "stream", stream_id, {"owner": stream.get("user_id")})
        raise DomainError(ErrorCode.FORBIDDEN, "无权访问该流", 403)
    # 流所属会话同样必须可用（过期/删除中拒绝）。
    validate_session_owner(store, ctx, stream["thread_id"])
    return stream
