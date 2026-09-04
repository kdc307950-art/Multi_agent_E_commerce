"""安全回归测试。

对应本次强化的安全红线：
1. Mock 令牌在 preview/production 禁用，真实 JWT 认证接口，生产 fail-closed。
2. 同租户跨用户越权修复：sessions / streams / approvals / operations / orders。
3. 过期、删除中会话拒绝。
4. query_order / track_shipping 缺少回复边修复。
5. CORS / 统一反向代理。
6. 每个安全拒绝路径补审计。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from src.auth.security import (
    _redact_detail,
    audit_security_denial,
    issue_login_token,
    issue_token,
    resolve_tenant_context,
)
from src.config import Settings
from src.core.types import DomainError, Role, SessionStatus
from src.graph.builder import build_graph
from src.infrastructure.store import MemoryStore
from src.llm.mock import MockLLM
from src.main import create_app
from tests.conftest import bearer


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _shared_store() -> MemoryStore:
    store = MemoryStore()
    store.create_tenant("TENANT-A", "租户A")
    store.create_tenant("TENANT-B", "租户B")
    store.add_membership("TENANT-A", "USER-001", Role.CUSTOMER)
    store.add_membership("TENANT-A", "USER-002", Role.CUSTOMER)
    store.add_membership("TENANT-A", "ADMIN-A", Role.ADMIN)
    store.add_membership("TENANT-A", "APPROVER-A", Role.APPROVER)
    store.add_membership("TENANT-B", "USER-B1", Role.CUSTOMER)
    store.add_membership("TENANT-B", "ADMIN-B", Role.ADMIN)
    return store


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _make_jwt(secret: str, claims: dict) -> str:
    header = _b64u(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64u(json.dumps(claims).encode())
    sig = hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64u(sig)}"


def _session(client, token) -> str:
    r = client.post("/api/sessions", headers=bearer(token))
    assert r.status_code == 201, r.text
    return r.json()["thread_id"]


def _approval_from_refund(client, token, thread_id, req_id="REQ-SEC") -> tuple[str, str]:
    import json as _json

    resp = client.post("/api/chat", json={
        "mode": "start", "thread_id": thread_id,
        "client_request_id": req_id, "message": "我要退款，订单号 ORD-001",
    }, headers=bearer(token))
    assert resp.status_code == 200, resp.text
    frames = []
    for block in resp.text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        d: dict = {}
        for line in block.split("\n"):
            if line.startswith("event: "):
                d["event"] = line[7:]
            elif line.startswith("data: "):
                d["data"] = _json.loads(line[6:])
        if "event" in d:
            frames.append(d)
    ar = next(f for f in frames if f["event"] == "approval_required")
    return ar["data"]["approval_id"], ar["data"]["operation_id"]


# ---------------------------------------------------------------------------
# 1. Mock 令牌在受限环境禁用 / 真实 JWT 认证 / 生产 fail-closed
# ---------------------------------------------------------------------------
def test_mock_auth_disabled_in_production_fail_closed():
    store = MemoryStore()
    store.create_tenant("T", "t")
    store.add_membership("T", "U", Role.CUSTOMER)
    with pytest.raises(DomainError) as ei:
        resolve_tenant_context(store, "Bearer mock:T:U:customer:abc",
                               settings=Settings(env="production", auth_backend="mock"))
    assert ei.value.code == "auth_backend_disabled"
    assert ei.value.status_code == 503


def test_mock_auth_still_ok_in_development():
    store = MemoryStore()
    store.create_tenant("T", "t")
    store.add_membership("T", "U", Role.CUSTOMER)
    ctx = resolve_tenant_context(store, "Bearer mock:T:U:customer:abc",
                                 settings=Settings(env="development", auth_backend="mock"))
    assert (ctx.tenant_id, ctx.user_id, ctx.role.value) == ("T", "U", "customer")


def test_create_app_fails_closed_on_mock_in_production():
    with pytest.raises(RuntimeError):
        create_app(settings=Settings(env="production", auth_backend="mock"))
    # development + mock 不抛，允许构建（本地开发/测试）。
    create_app(settings=Settings(env="development", auth_backend="mock"))


def test_create_app_fails_closed_on_missing_jwt_in_restricted_env():
    # preview + real + 缺 AUTH_JWT_SECRET → 启动即失败（fail-closed，不允许"服务在跑但认证永远 503"）。
    with pytest.raises(RuntimeError):
        create_app(settings=Settings(env="preview", auth_backend="real", auth_jwt_secret=""))
    with pytest.raises(RuntimeError):
        create_app(settings=Settings(env="production", auth_backend="real", auth_jwt_secret=""))
    # preview + real + 提供 secret → 允许构建。
    create_app(settings=Settings(env="preview", auth_backend="real", auth_jwt_secret="s3cret-verify"))


def test_real_jwt_auth_backend_verifies_signature():
    secret = "s3cret-very-secret"
    store = _shared_store()
    settings = Settings(env="production", auth_backend="real",
                        auth_jwt_secret=secret, auth_jwt_issuer="iss", auth_jwt_audience="aud")
    claims = {"sub": "USER-001", "tenant_id": "TENANT-A", "role": "customer",
              "exp": time.time() + 600, "iss": "iss", "aud": "aud"}
    token = _make_jwt(secret, claims)
    ctx = resolve_tenant_context(store, "Bearer " + token, settings=settings)
    assert (ctx.tenant_id, ctx.user_id, ctx.role.value) == ("TENANT-A", "USER-001", "customer")

    # 篡改签名 → 401（fail-closed，不降级到 mock）
    with pytest.raises(DomainError) as ei:
        resolve_tenant_context(store, "Bearer " + _make_jwt("wrong-secret", claims), settings=settings)
    assert ei.value.code == "unauthorized"


def test_real_jwt_requires_secret_else_fail_closed():
    store = _shared_store()
    with pytest.raises(DomainError) as ei:
        resolve_tenant_context(store, "Bearer xxx",
                               settings=Settings(env="production", auth_backend="real", auth_jwt_secret=""))
    assert ei.value.code == "auth_backend_disabled"


# ---------------------------------------------------------------------------
# 2. 同租户跨用户越权修复（sessions/streams/approvals/operations/orders）＋ 6. 审计
# ---------------------------------------------------------------------------
def test_same_tenant_customer_cannot_list_other_sessions(client, store):
    tok_a = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
    tok_b = issue_token("TENANT-A", "USER-002", Role.CUSTOMER)
    thread_a = _session(client, tok_a)
    sessions = client.get("/api/sessions", headers=bearer(tok_b)).json()
    assert all(s["thread_id"] != thread_a for s in sessions)
    # 单点越权访问也拒绝（同租户跨用户 → 403；不泄露会话内容）。
    assert client.get(f"/api/sessions/{thread_a}", headers=bearer(tok_b)).status_code == 403


def test_same_tenant_customer_cannot_resume_other_stream(client):
    tok_a = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
    tok_b = issue_token("TENANT-A", "USER-002", Role.CUSTOMER)
    thread_b = _session(client, tok_b)
    # 用户 B 生成一个流。
    start = client.post("/api/chat", json={
        "mode": "start", "thread_id": thread_b,
        "client_request_id": "REQ-STREAM-B", "message": "退货政策",
    }, headers=bearer(tok_b))
    import json as _json
    stream_id = None
    for block in start.text.split("\n\n"):
        if 'event: accepted' in block:
            for line in block.split("\n"):
                if line.startswith("data: "):
                    stream_id = _json.loads(line[6:])["stream_id"]
    assert stream_id
    # 用户 A 尝试 resume 用户 B 的流 → 403（跨用户拒绝）。
    r = client.post("/api/chat", json={"mode": "resume", "stream_id": stream_id},
                    headers={**bearer(tok_a), "Last-Event-ID": "0"})
    assert r.status_code == 403


def test_same_tenant_customer_cannot_read_other_approval_or_operation(client, store):
    tok_a = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
    tok_b = issue_token("TENANT-A", "USER-002", Role.CUSTOMER)
    thread_a = _session(client, tok_a)
    approval_id, operation_id = _approval_from_refund(client, tok_a, thread_a)
    # B 读 A 的审批 → 404（跨用户，不泄露存在）。
    assert client.get(f"/api/approvals/{approval_id}", headers=bearer(tok_b)).status_code == 404
    # B 读 A 的操作 → 404。
    assert client.get(f"/api/operations/{operation_id}", headers=bearer(tok_b)).status_code == 404
    # B 列审批不应包含 A 的审批。
    approvals = client.get("/api/approvals", headers=bearer(tok_b)).json()
    assert all(a["approval_id"] != approval_id for a in approvals)
    # 跨用户拒绝路径应写入审计。
    actions = [r.action for r in store.list_audit("TENANT-A")]
    assert any(a.startswith("security.deny.") for a in actions)


def test_same_tenant_customer_cannot_read_other_order(client, store):
    tok_a = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
    tok_b = issue_token("TENANT-A", "USER-002", Role.CUSTOMER)
    # ORD-001 属于 USER-001（TENANT-A）。
    assert client.get("/api/orders/ORD-001", headers=bearer(tok_a)).status_code == 200
    # USER-002 查同一租户的他人订单 → 404（不认为是"存在但无权"）。
    assert client.get("/api/orders/ORD-001", headers=bearer(tok_b)).status_code == 404
    # staff 可查。
    tok_admin = issue_token("TENANT-A", "ADMIN-A", Role.ADMIN)
    assert client.get("/api/orders/ORD-001", headers=bearer(tok_admin)).status_code == 200
    actions = [r.action for r in store.list_audit("TENANT-A")]
    assert any("security.deny.cross_user_order" in a for a in actions)


def test_staff_can_access_same_tenant_other_user_resources(client):
    tok_a = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
    tok_admin = issue_token("TENANT-A", "ADMIN-A", Role.ADMIN)
    thread_a = _session(client, tok_a)
    assert client.get(f"/api/sessions/{thread_a}", headers=bearer(tok_admin)).status_code == 200


# ---------------------------------------------------------------------------
# 3. 过期 / 删除中会话拒绝
# ---------------------------------------------------------------------------
def test_expired_session_rejected(client, store):
    tok = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
    thread = _session(client, tok)
    store.mark_session_status("TENANT-A", thread, SessionStatus.EXPIRED)
    # 直接读会话 → 410；聊天 start → 410；消息历史 → 410。
    assert client.get(f"/api/sessions/{thread}", headers=bearer(tok)).status_code == 410
    r = client.post("/api/chat", json={
        "mode": "start", "thread_id": thread,
        "client_request_id": "REQ-EXP", "message": "你好",
    }, headers=bearer(tok))
    assert r.status_code == 410
    assert client.get(f"/api/sessions/{thread}/messages", headers=bearer(tok)).status_code == 410


def test_deleting_session_rejected(client, store):
    tok = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
    thread = _session(client, tok)
    store.mark_session_status("TENANT-A", thread, SessionStatus.DELETING)
    assert client.get(f"/api/sessions/{thread}", headers=bearer(tok)).status_code == 403
    r = client.post("/api/chat", json={
        "mode": "start", "thread_id": thread,
        "client_request_id": "REQ-DEL", "message": "你好",
    }, headers=bearer(tok))
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# 4. query_order / track_shipping 回复边
# ---------------------------------------------------------------------------
def test_query_order_and_track_shipping_produce_reply():
    store = MemoryStore()
    store.create_tenant("TENANT-A", "a")
    store.add_membership("TENANT-A", "USER-001", Role.CUSTOMER)
    store.add_membership("TENANT-A", "ADMIN-A", Role.ADMIN)
    g = build_graph(MockLLM(), store, InMemorySaver())
    base = {"tenant_id": "TENANT-A", "thread_id": "th", "client_request_id": "c", "model": "gpt-4"}

    out = g.invoke({**base, "messages": [{"role": "user", "content": "查询订单 ORD-001"}],
                    "user_id": "USER-001", "role": "customer"},
                   config={"configurable": {"thread_id": "th"}})
    assert out.get("final_response")  # 不再为 None（回复边已补）。

    out2 = g.invoke({**base, "messages": [{"role": "user", "content": "查询物流 ORD-001"}],
                     "user_id": "USER-001", "role": "customer", "thread_id": "th2"},
                    config={"configurable": {"thread_id": "th2"}})
    assert out2.get("final_response")


def test_query_order_hides_other_user_order_in_graph():
    store = MemoryStore()
    store.create_tenant("TENANT-A", "a")
    store.add_membership("TENANT-A", "USER-001", Role.CUSTOMER)
    store.add_membership("TENANT-A", "USER-002", Role.CUSTOMER)
    store.add_membership("TENANT-A", "ADMIN-A", Role.ADMIN)
    g = build_graph(MockLLM(), store, InMemorySaver())

    # USER-002 查 USER-001 的订单 → 视为"不存在"。
    out = g.invoke({"messages": [{"role": "user", "content": "查询订单 ORD-001"}],
                    "tenant_id": "TENANT-A", "user_id": "USER-002", "role": "customer",
                    "thread_id": "th-b", "client_request_id": "cb", "model": "gpt-4"},
                   config={"configurable": {"thread_id": "th-b"}})
    assert "不存在" in out["final_response"]

    # staff 仍可见。
    out2 = g.invoke({"messages": [{"role": "user", "content": "查询订单 ORD-001"}],
                     "tenant_id": "TENANT-A", "user_id": "ADMIN-A", "role": "admin",
                     "thread_id": "th-a", "client_request_id": "ca", "model": "gpt-4"},
                    config={"configurable": {"thread_id": "th-a"}})
    assert "不存在" not in out2["final_response"]


# ---------------------------------------------------------------------------
# 5. CORS / 统一反向代理
# ---------------------------------------------------------------------------
def test_cors_preflight_when_enabled_with_origin_whitelist():
    store = _shared_store()
    app = create_app(
        store=store, llm=MockLLM(), seed=False,
        settings=Settings(enable_cors=True, cors_allow_origins="http://localhost:3000"),
    )
    with TestClient(app) as c:
        r = c.options("/api/sessions", headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
        })
        assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_cors_disabled_by_default():
    store = _shared_store()
    app = create_app(store=store, llm=MockLLM(), seed=False,
                     settings=Settings(enable_cors=False))
    with TestClient(app) as c:
        r = c.options("/api/sessions", headers={
            "Origin": "http://evil.local",
            "Access-Control-Request-Method": "POST",
        })
        # 未启用 CORS → 不返回允许跨域头。
        assert r.headers.get("access-control-allow-origin") != "http://evil.local"


# ---------------------------------------------------------------------------
# 7. 登录凭据哈希升级：Argon2id 默认校验；bcrypt 可选；旧无盐 SHA-256 一律拒绝（legacy_sha256）
# ---------------------------------------------------------------------------
def _login_store() -> MemoryStore:
    store = MemoryStore()
    store.create_tenant("T", "t")
    store.add_membership("T", "U", Role.CUSTOMER)
    return store


def _real_settings(creds_json: str, algo: str = "argon2id") -> Settings:
    return Settings(env="production", auth_backend="real", auth_jwt_secret="sec-please",
                    auth_login_credentials=creds_json, auth_credential_hash=algo)


def test_login_verifies_argon2id_and_returns_jwt():
    from argon2 import PasswordHasher

    phc = PasswordHasher().hash("correct-pw")
    store = _login_store()
    settings = _real_settings(json.dumps({"T:U": phc}), "argon2id")
    token = issue_login_token(store, settings, "T", "U", "correct-pw")
    assert token.count(".") == 2 and not token.startswith("mock:")


def test_login_rejects_legacy_sha256_with_audit():
    # 服务端仍存旧无盐 SHA-256 → 一律拒绝并在审计中标注 legacy_sha256（弱哈希已消除）。
    legacy = hashlib.sha256(b"correct-pw").hexdigest()
    store = _login_store()
    settings = _real_settings(json.dumps({"T:U": legacy}), "argon2id")
    with pytest.raises(DomainError) as ei:
        issue_login_token(store, settings, "T", "U", "correct-pw")
    assert ei.value.code == "unauthorized" and ei.value.status_code == 401
    actions = [r.action for r in store.list_audit("T")]
    assert any(a.startswith("security.deny.legacy_sha256") for a in actions)


def test_login_argon2id_wrong_password_rejected():
    from argon2 import PasswordHasher

    phc = PasswordHasher().hash("correct-pw")
    store = _login_store()
    settings = _real_settings(json.dumps({"T:U": phc}), "argon2id")
    with pytest.raises(DomainError) as ei:
        issue_login_token(store, settings, "T", "U", "wrong-pw")
    assert ei.value.status_code == 401
    assert any(a.startswith("security.deny.login_failed") for a in [r.action for r in store.list_audit("T")])


def test_login_bcrypt_scheme_verifies():
    import bcrypt

    phc = bcrypt.hashpw(b"correct-pw", bcrypt.gensalt()).decode("utf-8")
    store = _login_store()
    settings = _real_settings(json.dumps({"T:U": phc}), "bcrypt")
    token = issue_login_token(store, settings, "T", "U", "correct-pw")
    assert token.count(".") == 2
    # 反向验证：bcrypt 表 + 错误口令 → 401。
    with pytest.raises(DomainError) as ei:
        issue_login_token(store, settings, "T", "U", "wrong-pw")
    assert ei.value.status_code == 401


# ---------------------------------------------------------------------------
# 8. 脱敏黑名单补 hash/phc/credential_hash：凭据哈希永不入审计日志
# ---------------------------------------------------------------------------
def test_redact_detail_masks_hash_phc_credential_hash_keys():
    # 若未来某路径把凭据哈希写入名为 hash/phc/credential_hash 的 detail 键，应被 [REDACTED]。
    leaked = _redact_detail({
        "hash": "deadbeef",
        "phc": "$argon2id$v=19$m=65536,t=3,p=4$salt$tag",
        "credential_hash": "zzz",
        "algorithm": "argon2id",           # 非敏感键保持原样
        "nested": {"pwd_hash": "nested-hash"},
    })
    assert leaked["hash"] == "[REDACTED]"
    assert leaked["phc"] == "[REDACTED]"
    assert leaked["credential_hash"] == "[REDACTED]"
    assert leaked["algorithm"] == "argon2id"
    # 子串匹配也应命中嵌套/前缀键（password_hash / hashed_credential）。
    assert leaked["nested"] == {"pwd_hash": "[REDACTED]"}


def test_audit_security_denial_redacts_hash_phc_credential_hash():
    store = _login_store()
    audit_security_denial(store, "T", "U", "login_failed", "auth", "T:U",
                          {"hash": "deadbeef", "phc": "$argon2id$.", "credential_hash": "zzz",
                           "client_ip": "10.0.0.1", "algorithm": "argon2id"})
    records = store.list_audit("T")
    assert records, "应写入审计记录"
    rec = records[-1]
    blob = str(rec.detail)
    assert "[REDACTED]" in blob
    assert "deadbeef" not in blob and "$argon2id" not in blob and "zzz" not in blob
    # 非敏感键保持原文。
    assert rec.detail.get("client_ip") == "10.0.0.1"
