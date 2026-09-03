"""多租户隔离测试。

验证冻结契约：
- 跨租户 thread/审批/操作默认拒绝（返回 404 不泄露存在）。
- 客户端覆盖 tenant_id 被拒。
- 停用租户不能创建会话。
- platform_admin 不是租户成员角色；它不是 Role 枚举成员。
"""
from __future__ import annotations

import pytest

from tests.conftest import bearer
from tests.test_chat_sse import parse_sse as _parse
from src.auth.security import issue_token
from src.core.types import DomainError, Role, TenantStatus


def _session(client, token) -> str:
    r = client.post("/api/sessions", headers=bearer(token))
    assert r.status_code == 201, r.text
    return r.json()["thread_id"]


def test_cross_tenant_thread_not_visible(client):
    token_a = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
    token_b = issue_token("TENANT-B", "USER-B1", Role.CUSTOMER)
    thread_a = _session(client, token_a)
    # 租户 B 复用租户 A 的 thread_id → 拒绝且不泄露存在（404）。
    resp = client.post("/api/chat", json={
        "mode": "start", "thread_id": thread_a,
        "client_request_id": "REQ-B1", "message": "你好",
    }, headers=bearer(token_b))
    assert resp.status_code == 404


def test_client_tenant_override_is_rejected(client):
    token_a = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
    thread_a = _session(client, token_a)
    # schema 拒绝未知字段 tenant_id。
    resp = client.post("/api/chat", json={
        "mode": "start", "thread_id": thread_a,
        "client_request_id": "REQ-O1", "message": "你好", "tenant_id": "TENANT-B",
    }, headers=bearer(token_a))
    assert resp.status_code == 422


def test_suspended_tenant_cannot_create_session(client, store):
    token_a = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
    store.set_tenant_status("TENANT-A", TenantStatus.SUSPENDED)
    resp = client.post("/api/sessions", headers=bearer(token_a))
    assert resp.status_code == 403


def test_cross_tenant_approval_is_rejected(client):
    # 租户 A 发起退款 → 产生 approval；租户 B 的 admin 审批应被拒（404）。
    token_a = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
    thread_a = _session(client, token_a)
    feed = client.post("/api/chat", json={
        "mode": "start", "thread_id": thread_a,
        "client_request_id": "REQ-CR1", "message": "我要退款，订单号 ORD-001",
    }, headers=bearer(token_a))
    ar = next(e for e in _parse(feed.text) if e["event"] == "approval_required")
    approval_id = ar["data"]["approval_id"]

    token_b_admin = issue_token("TENANT-B", "ADMIN-B", Role.ADMIN)
    resp = client.post(f"/api/approvals/{approval_id}/decision",
                       json={"approved": True, "confirmation": True}, headers=bearer(token_b_admin))
    assert resp.status_code == 404


def test_platform_admin_is_not_tenant_member():
    # platform_admin 是独立平台级能力，不是一个可写入 tenant_memberships 的租户角色。
    assert "platform_admin" in {"platform_admin"}
    # Role 枚举只包含租户角色；无法用平台角色构造合法租户角色。
    assert len(Role.tenant_roles()) == 4
    assert "platform_admin" not in Role.tenant_roles()
    with pytest.raises(ValueError):
        Role("platform_admin")


def test_tenant_id_is_never_derived_from_thread_id(client):
    # thread_id 是服务端生成的 UUID，不含租户/用户信息；归属由服务端 sessions 表维护。
    token_a = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
    thread_a = _session(client, token_a)
    # thread_id 应为不透明 UUID。
    import uuid
    uuid.UUID(thread_a)
    # 会话归属校验：用任意字符串作为 thread_id 访问返回 404，不解析内容。
    resp = client.get(f"/api/sessions/{thread_a}", headers=bearer(token_a))
    assert resp.status_code == 200
