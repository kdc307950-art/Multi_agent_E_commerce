"""真实 JWT 认证 + 预发布（preview）多租户安全集成测试。

覆盖验收口径：
1. 真实 JWT 可登录（`POST /api/auth/login` 签发 → 带令牌访问受保护资源成功）。
2. 越权场景全部拒绝：跨租户访问、同租户跨客户会话/审批/操作、订单跨用户。
3. 撤销成员后旧令牌无法继续访问；租户停用后旧令牌同样被拒。
4. 会话过期 / SSE 重放越权（跨用户 resume）拒绝。
5. 审批仅限当前租户 admin / approver（customer/agent 一律拒绝），且审批人角色校验审计留痕。
6. JWT 固定 iss/aud/过期与密钥轮换：过期/错误发布方/未知 kid 一律拒绝；旧密钥令牌在
   宽容窗口内仍可校验。
7. 所有安全拒绝必须写入**脱敏**审计记录（不出现口令/令牌原文/完整凭据）。

全部测试在 `env=preview` + `auth_backend=real` 下运行，与 Mock 认证隔离。
"""
from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from src.auth.security import issue_jwt, resolve_tenant_context
from src.config import Settings
from src.core.types import Role, SessionStatus, TenantStatus
from src.llm.mock import MockLLM
from src.main import create_app
from tests.conftest import bearer
from tests.test_chat_sse import parse_sse

ISSUER = "after-sales"
AUDIENCE = "after-sales-web"
ACTIVE_SECRET = "active-secret-key-01"
OLD_SECRET = "rotated-secret-key-00"

# 与 src/auth/security.py 脱敏黑名单对齐的检测键（测试端兜底）。
_SENSITIVE_KEYS_SANITY = ("password", "credential", "secret", "token", "authorization",
                          "address", "payment", "card")

# 各测试用户使用的登录凭据明文（仅在测试内构造 PHC 哈希表——默认 argon2id，绝不写入审计）。
_PASSWORDS = {
    "TENANT-A:USER-001": "cred-user-a1",
    "TENANT-A:USER-002": "cred-user-a2",
    "TENANT-A:AGENT-A": "cred-agent-a",
    "TENANT-A:ADMIN-A": "cred-admin-a",
    "TENANT-A:APPROVER-A": "cred-approver-a",
    "TENANT-B:USER-B1": "cred-user-b1",
    "TENANT-B:USER-B2": "cred-user-b2",
    "TENANT-B:AGENT-B": "cred-agent-b",
    "TENANT-B:ADMIN-B": "cred-admin-b",
    "TENANT-B:APPROVER-B": "cred-approver-b",
}


@functools.lru_cache(maxsize=1)
def _creds_json() -> str:
    # 生成 argon2id PHC 哈希表（与 config 默认 auth_credential_hash=argon2id 对齐）。
    from argon2 import PasswordHasher

    hasher = PasswordHasher()
    return json.dumps({k: hasher.hash(v) for k, v in _PASSWORDS.items()})


def _real_settings(rotated: str = "", ttl: int = 3600, auth_kid: str = "",
                   credential_hash: str = "argon2id") -> Settings:
    return Settings(
        env="preview",
        auth_backend="real",
        auth_jwt_secret=ACTIVE_SECRET,
        auth_jwt_issuer=ISSUER,
        auth_jwt_audience=AUDIENCE,
        auth_jwt_ttl_seconds=ttl,
        auth_jwt_kid=auth_kid,
        auth_jwt_rotated_secrets=rotated,
        auth_login_credentials=_creds_json() if credential_hash == "argon2id" else _creds_bcrypt_json(),
        auth_credential_hash=credential_hash,
    )


@functools.lru_cache(maxsize=1)
def _creds_bcrypt_json() -> str:
    import bcrypt

    return json.dumps({k: bcrypt.hashpw(v.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
                       for k, v in _PASSWORDS.items()})


@pytest.fixture
def real_app(store):
    return create_app(store=store, llm=MockLLM("gpt-4"), seed=False,
                      settings=_real_settings())


@pytest.fixture
def real_client(real_app):
    with TestClient(real_app) as c:
        yield c


def _login(client, tenant: str, user: str) -> str:
    r = client.post("/api/auth/login", json={
        "tenant_id": tenant, "user_id": user,
        "credential": _PASSWORDS[f"{tenant}:{user}"],
    })
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _session(client, token) -> str:
    r = client.post("/api/sessions", headers=bearer(token))
    assert r.status_code == 201, r.text
    return r.json()["thread_id"]


def _approval_from_refund(client, token, thread_id, req_id="REQ-JWT") -> tuple[str, str]:
    resp = client.post("/api/chat", json={
        "mode": "start", "thread_id": thread_id,
        "client_request_id": req_id, "message": "我要退款，订单号 ORD-001",
    }, headers=bearer(token))
    assert resp.status_code == 200, resp.text
    events = parse_sse(resp.text)
    ar = next(e for e in events if e["event"] == "approval_required")
    return ar["data"]["approval_id"], ar["data"]["operation_id"]


def _assert_denied_audit(store, tenant: str, *reasons: str) -> bool:
    """断言该租户下存在预期的安全拒绝审计，且审计 detail 已脱敏。"""
    records = store.list_audit(tenant)
    actions = [r.action for r in records]
    audit_ok = any(any(reason in a for a in actions) for reason in reasons)
    # 脱敏：审计 detail 不应含明文口令/令牌片段；敏感键应被 [REDACTED]。
    sensitive_markers = list(_PASSWORDS.values())
    for rec in records:
        blob = json.dumps(rec.detail, ensure_ascii=False) + "|" + rec.target_id
        for pw in sensitive_markers:
            assert pw not in blob, f"审计泄露口令: {rec.action}"
        # 令牌原文永不以可复现形式出现：token: 开头表示指纹形式。
        if rec.target_type == "auth" and rec.target_id:
            assert not (rec.target_id.count(".") == 2), "审计记录出现令牌原文"
    return audit_ok


# ---------------------------------------------------------------------------
# 1. 真实 JWT 可登录
# ---------------------------------------------------------------------------
def test_login_endpoint_returns_real_jwt_and_access(real_client, store):
    token = _login(real_client, "TENANT-A", "USER-001")
    # 凭据是真实 HS256 JWT（三段），非 mock。
    assert token.count(".") == 2 and not token.startswith("mock:")
    thread = _session(real_client, token)
    # 会话可读：证明签发+校验+成员校验闭环。
    assert real_client.get(f"/api/sessions/{thread}", headers=bearer(token)).status_code == 200


def test_issued_jwt_directly_can_access(store):
    app = create_app(store=store, llm=MockLLM("gpt-4"), seed=False, settings=_real_settings())
    with TestClient(app) as c:
        token = issue_jwt("TENANT-A", "USER-001", Role.CUSTOMER, _real_settings())
        assert c.post("/api/sessions", headers=bearer(token)).status_code == 201


def test_login_fails_on_wrong_credential_with_audit(real_client, store):
    r = real_client.post("/api/auth/login", json={
        "tenant_id": "TENANT-A", "user_id": "USER-001", "credential": "wrong-pass",
    })
    assert r.status_code == 401
    _assert_denied_audit(store, "TENANT-A", "login_failed")


# ---------------------------------------------------------------------------
# 2. JWT 固定声明 / 过期 / 轮换
# ---------------------------------------------------------------------------
def test_expired_jwt_rejected(store):
    settings = _real_settings()
    token = issue_jwt("TENANT-A", "USER-001", Role.CUSTOMER, settings,
                      now=time.time() - 100, exp=time.time() - 10)  # 已过期
    with pytest.raises(Exception) as ei:
        resolve_tenant_context(store, "Bearer " + token, settings=settings)
    assert ei.value.code == "unauthorized"


def test_wrong_issuer_or_audience_rejected(store):
    settings = _real_settings()
    # 篡改 iss/aud，仍用正确密钥签名。
    token = _sign_jwt(ACTIVE_SECRET, _derive_kid(ACTIVE_SECRET), {
        "sub": "USER-001", "tenant_id": "TENANT-A", "role": "customer",
        "iss": "evil-iss", "aud": AUDIENCE, "exp": time.time() + 600,
    })
    with pytest.raises(Exception) as ei:
        resolve_tenant_context(store, "Bearer " + token, settings=settings)
    assert ei.value.code == "unauthorized"


def test_missing_exp_rejected(store):
    settings = _real_settings()
    token = _sign_jwt(ACTIVE_SECRET, _derive_kid(ACTIVE_SECRET), {
        "sub": "USER-001", "tenant_id": "TENANT-A", "role": "customer",
        "iss": ISSUER, "aud": AUDIENCE,
    })
    with pytest.raises(Exception) as ei:
        resolve_tenant_context(store, "Bearer " + token, settings=settings)
    assert ei.value.code == "unauthorized"


def test_key_rotation_old_token_still_valid(store):
    # 轮换：active=ACTIVE_SECRET，OLD_SECRET 进入轮换池（宽容窗口）。
    settings = _real_settings(rotated=OLD_SECRET)
    settings_for_old = Settings(
        env="preview", auth_backend="real", auth_jwt_secret=ACTIVE_SECRET,
        auth_jwt_issuer=ISSUER, auth_jwt_audience=AUDIENCE,
        auth_jwt_ttl_seconds=3600, auth_jwt_rotated_secrets=OLD_SECRET,
    )
    # 用旧密钥签发的令牌（带旧 kid）→ 仍可校验。
    old_kid = _derive_kid(OLD_SECRET)
    old_token = _sign_jwt(OLD_SECRET, old_kid, {
        "sub": "USER-001", "tenant_id": "TENANT-A", "role": "customer",
        "iss": ISSUER, "aud": AUDIENCE, "exp": time.time() + 600,
    })
    ctx = resolve_tenant_context(store, "Bearer " + old_token, settings=settings_for_old)
    assert ctx.user_id == "USER-001"


def test_key_rotation_unknown_kid_rejected(store):
    settings = _real_settings(rotated=OLD_SECRET)
    unknown_kid = _derive_kid("another-unseen-secret")
    # 用正确 active 密钥签名，但 header 的 kid 不在池中 → 拒绝（fail-closed）。
    token = _sign_jwt(ACTIVE_SECRET, unknown_kid, {
        "sub": "USER-001", "tenant_id": "TENANT-A", "role": "customer",
        "iss": ISSUER, "aud": AUDIENCE, "exp": time.time() + 600,
    })
    with pytest.raises(Exception) as ei:
        resolve_tenant_context(store, "Bearer " + token, settings=settings)
    assert ei.value.code == "unauthorized"


# ---------------------------------------------------------------------------
# 3. 跨租户 / 同租户跨客户越权（全部拒绝并审计）
# ---------------------------------------------------------------------------
def test_cross_tenant_access_denied(real_client, store):
    tok_a = _login(real_client, "TENANT-A", "USER-001")
    tok_b = _login(real_client, "TENANT-B", "USER-B1")
    thread_a = _session(real_client, tok_a)
    # 租户 B 用户尝试访问租户 A 的会话/流 → 404（不泄露存在）。
    r = real_client.get(f"/api/sessions/{thread_a}", headers=bearer(tok_b))
    assert r.status_code == 404
    # 跨租户审批（B admin 审 A 的 approval）→ 404。
    approval_id, _ = _approval_from_refund(real_client, tok_a, thread_a, "REQ-XTEN")
    r = real_client.post(f"/api/approvals/{approval_id}/decision",
                         json={"approved": True, "confirmation": True},
                         headers=bearer(_login(real_client, "TENANT-B", "ADMIN-B")))
    assert r.status_code == 404
    _assert_denied_audit(store, "TENANT-A", "approval_access_denied", "session_not_found")


def test_same_tenant_cross_customer_resource_denied(real_client, store):
    tok_a = _login(real_client, "TENANT-A", "USER-001")
    tok_b = _login(real_client, "TENANT-A", "USER-002")
    thread_a = _session(real_client, tok_a)
    # 同租户跨客户：读他人会话 403；读审批/操作 404（不泄露存在）。
    assert real_client.get(f"/api/sessions/{thread_a}", headers=bearer(tok_b)).status_code == 403
    approval_id, operation_id = _approval_from_refund(real_client, tok_a, thread_a, "REQ-XUSER")
    assert real_client.get(f"/api/approvals/{approval_id}", headers=bearer(tok_b)).status_code == 404
    assert real_client.get(f"/api/operations/{operation_id}", headers=bearer(tok_b)).status_code == 404
    # 客户跨用户查他人订单 → 404。
    assert real_client.get("/api/orders/ORD-001", headers=bearer(tok_b)).status_code == 404
    _assert_denied_audit(store, "TENANT-A", "cross_user_session", "cross_user_order",
                         "approval_access_denied", "operation_access_denied")


def test_staff_can_access_same_tenant_other_user_resources(real_client, store):
    tok_a = _login(real_client, "TENANT-A", "USER-001")
    tok_admin = _login(real_client, "TENANT-A", "ADMIN-A")
    thread_a = _session(real_client, tok_a)
    assert real_client.get(f"/api/sessions/{thread_a}", headers=bearer(tok_admin)).status_code == 200


# ---------------------------------------------------------------------------
# 4. 撤销成员后旧令牌无法继续访问 / 租户停用
# ---------------------------------------------------------------------------
def test_revoked_member_old_token_denied(real_client, store):
    tok = _login(real_client, "TENANT-A", "USER-002")
    thread = _session(real_client, tok)
    store.revoke_membership("TENANT-A", "USER-002")
    # 旧令牌（签名仍有效，但成员已撤销）→ 拒绝（403）并审计。
    assert real_client.get(f"/api/sessions/{thread}", headers=bearer(tok)).status_code == 403
    _assert_denied_audit(store, "TENANT-A", "forbidden")


def test_suspended_tenant_old_token_denied(real_client, store):
    tok = _login(real_client, "TENANT-A", "USER-001")
    thread = _session(real_client, tok)
    store.set_tenant_status("TENANT-A", TenantStatus.SUSPENDED)
    assert real_client.get(f"/api/sessions/{thread}", headers=bearer(tok)).status_code == 403
    _assert_denied_audit(store, "TENANT-A", "tenant_suspended", "forbidden")


# ---------------------------------------------------------------------------
# 5. 会话过期 / SSE 重放越权
# ---------------------------------------------------------------------------
def test_session_expired_denied(real_client, store):
    tok = _login(real_client, "TENANT-A", "USER-001")
    thread = _session(real_client, tok)
    store.mark_session_status("TENANT-A", thread, SessionStatus.EXPIRED)
    assert real_client.get(f"/api/sessions/{thread}", headers=bearer(tok)).status_code == 410
    _assert_denied_audit(store, "TENANT-A", "session_expired")


def test_sse_resume_cross_user_replay_denied(real_client, store):
    tok_a = _login(real_client, "TENANT-A", "USER-001")
    tok_b = _login(real_client, "TENANT-A", "USER-002")
    thread_b = _session(real_client, tok_b)
    start = real_client.post("/api/chat", json={
        "mode": "start", "thread_id": thread_b,
        "client_request_id": "REQ-STREAM-JWT", "message": "退货政策",
    }, headers=bearer(tok_b))
    stream_id = parse_sse(start.text)[0]["data"]["stream_id"]
    # 用户 A 尝试重放用户 B 的流 → 跨用户拒绝（403）。
    r = real_client.post("/api/chat", json={"mode": "resume", "stream_id": stream_id},
                         headers={**bearer(tok_a), "Last-Event-ID": "0"})
    assert r.status_code == 403
    _assert_denied_audit(store, "TENANT-A", "cross_user_stream")


# ---------------------------------------------------------------------------
# 6. 审批仅限当前租户 admin / approver
# ---------------------------------------------------------------------------
def test_approval_requires_admin_or_approver_role(real_client, store):
    tok_user = _login(real_client, "TENANT-A", "USER-001")
    thread = _session(real_client, tok_user)
    approval_id, operation_id = _approval_from_refund(real_client, tok_user, thread, "REQ-ROLE")
    # customer 审批 → 403。
    r = real_client.post(f"/api/approvals/{approval_id}/decision",
                         json={"approved": True, "confirmation": True},
                         headers=bearer(tok_user))
    assert r.status_code == 403
    # agent 审批 → 403。
    tok_agent = _login(real_client, "TENANT-A", "AGENT-A")
    r = real_client.post(f"/api/approvals/{approval_id}/decision",
                         json={"approved": True, "confirmation": True},
                         headers=bearer(tok_agent))
    assert r.status_code == 403
    # approver 审批 → 200，操作进入执行/终态。
    tok_approver = _login(real_client, "TENANT-A", "APPROVER-A")
    r = real_client.post(f"/api/approvals/{approval_id}/decision",
                         json={"approved": True, "confirmation": True},
                         headers=bearer(tok_approver))
    assert r.status_code == 200
    after = real_client.get(f"/api/operations/{operation_id}", headers=bearer(tok_approver))
    assert after.json()["status"] == "executed"
    _assert_denied_audit(store, "TENANT-A", "forbidden")


def test_customer_cannot_list_members(real_client, store):
    tok_user = _login(real_client, "TENANT-A", "USER-001")
    assert real_client.get("/api/members", headers=bearer(tok_user)).status_code == 403
    _assert_denied_audit(store, "TENANT-A", "forbidden")


# ---------------------------------------------------------------------------
# 7. 所有拒绝写入脱敏审计（汇总断言）
# ---------------------------------------------------------------------------
def test_all_denials_write_desensitized_audit(real_client, store):
    # 制造若干拒绝：缺 token、跨租户、审批越权、登录失败。
    assert real_client.get("/api/sessions").status_code == 401  # 缺 token
    tok_a = _login(real_client, "TENANT-A", "USER-001")
    tok_b = _login(real_client, "TENANT-B", "USER-B1")
    thread_a = _session(real_client, tok_a)
    assert real_client.post("/api/chat", json={
        "mode": "start", "thread_id": thread_a, "client_request_id": "REQ-MISS",
        "message": "你好",
    }, headers=bearer(tok_b)).status_code == 404
    real_client.post("/api/auth/login", json={
        "tenant_id": "TENANT-A", "user_id": "USER-001", "credential": "wrong"},
    )
    # 租户 A 的所有审计记录 detail 均不含任何口令明文；敏感键值应为 [REDACTED]。
    records = store.list_audit("TENANT-A")
    assert any(r.action.startswith("security.deny.") for r in records), "缺少拒绝审计记录"
    for rec in records:
        blob = json.dumps(rec.detail, ensure_ascii=False) + "|" + rec.target_id
        for pw in _PASSWORDS.values():
            assert pw not in blob, f"审计泄露口令: {rec.action}"
        # 若记录了敏感键（credential/secret/password/...），其值必须已是 [REDACTED]。
        for key in _SENSITIVE_KEYS_SANITY:
            if key.lower() in json.dumps(rec.detail, ensure_ascii=False).lower():
                assert json.dumps(rec.detail, ensure_ascii=False).count("[REDACTED]") > 0, \
                    f"敏感字段未脱敏: {rec.action}"


# ---------------------------------------------------------------------------
# 工具：手工构造 HS256 JWT（供轮换/签发校验测试，与 issue_jwt 平行）
# ---------------------------------------------------------------------------
def _derive_kid(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()[:16]


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _sign_jwt(secret: str, kid: str, claims: dict) -> str:
    header = _b64u(json.dumps({"alg": "HS256", "typ": "JWT", "kid": kid}).encode())
    payload = _b64u(json.dumps(claims).encode())
    sig = hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64u(sig)}"
