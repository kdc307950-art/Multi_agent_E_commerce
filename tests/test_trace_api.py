"""租户隔离执行轨迹与审批后 Shadow 事件合并测试。"""
from __future__ import annotations

import json

from src.auth.security import issue_token
from src.core.types import Role
from tests.conftest import bearer


def _frames(text: str) -> list[dict]:
    frames: list[dict] = []
    for block in text.strip().split("\n\n"):
        if not block.strip():
            continue
        frame: dict = {}
        for line in block.splitlines():
            if line.startswith("id: "):
                frame["id"] = int(line[4:])
            elif line.startswith("event: "):
                frame["event"] = line[7:]
            elif line.startswith("data: "):
                frame["data"] = json.loads(line[6:])
        if "event" in frame:
            frames.append(frame)
    return frames


def _start_refund(client, token: str, request_id: str) -> dict:
    session = client.post("/api/sessions", headers=bearer(token))
    assert session.status_code == 201
    response = client.post("/api/chat", headers=bearer(token), json={
        "mode": "start",
        "thread_id": session.json()["thread_id"],
        "client_request_id": request_id,
        "message": "我要退款，订单号 ORD-001",
    })
    assert response.status_code == 200
    frames = _frames(response.text)
    approval = next(frame for frame in frames if frame["event"] == "approval_required")
    return {
        "trace_id": approval["data"]["trace_id"],
        "approval_id": approval["data"]["approval_id"],
        "operation_id": approval["data"]["operation_id"],
        "approval_seq": approval["id"],
    }


def test_trace_query_is_tenant_and_owner_scoped(client, store):
    owner = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
    other_customer = issue_token("TENANT-A", "USER-002", Role.CUSTOMER)
    other_tenant = issue_token("TENANT-B", "USER-B1", Role.CUSTOMER)
    started = _start_refund(client, owner, "REQ-TRACE-SCOPE")

    own = client.get(f"/api/traces/{started['trace_id']}", headers=bearer(owner))
    assert own.status_code == 200
    assert own.json()["trace_id"] == started["trace_id"]
    assert own.json()["status"] == "waiting_approval"

    denied_user = client.get(f"/api/traces/{started['trace_id']}", headers=bearer(other_customer))
    assert denied_user.status_code == 403
    denied_subscribe = client.get(
        f"/api/traces/{started['trace_id']}/events?after_seq=0&wait_seconds=0",
        headers=bearer(other_customer),
    )
    assert denied_subscribe.status_code == 403

    denied_tenant = client.get(f"/api/traces/{started['trace_id']}", headers=bearer(other_tenant))
    assert denied_tenant.status_code == 404
    assert any(a.action == "security.deny.cross_user_stream" for a in store.list_audit("TENANT-A"))
    assert any(a.action == "security.deny.trace_access_denied" for a in store.list_audit("TENANT-B"))


def test_approval_appends_shadow_events_to_original_trace_once(client):
    owner = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
    approver = issue_token("TENANT-A", "APPROVER-A", Role.APPROVER)
    started = _start_refund(client, owner, "REQ-TRACE-SHADOW")

    before = client.get(
        f"/api/traces/{started['trace_id']}/events"
        f"?after_seq={started['approval_seq']}&wait_seconds=0",
        headers=bearer(owner),
    )
    assert before.status_code == 200
    assert _frames(before.text) == []

    decision = client.post(
        f"/api/approvals/{started['approval_id']}/decision",
        headers=bearer(approver),
        json={"approved": True, "confirmation": True,
              "operation_id": started["operation_id"], "pending_action": "refund"},
    )
    assert decision.status_code == 200

    after = client.get(
        f"/api/traces/{started['trace_id']}/events"
        f"?after_seq={started['approval_seq']}&wait_seconds=0",
        headers=bearer(owner),
    )
    frames = _frames(after.text)
    stages = [frame["data"]["stage"] for frame in frames]
    assert stages == ["shadow_started", "shadow_completed"]
    assert [frame["id"] for frame in frames] == sorted({frame["id"] for frame in frames})
    assert all(frame["data"]["trace_id"] == started["trace_id"] for frame in frames)
    assert frames[-1]["data"]["status"] == "passed"

    replay = client.post(
        f"/api/approvals/{started['approval_id']}/decision",
        headers=bearer(approver),
        json={"approved": True, "confirmation": True},
    )
    assert replay.status_code == 200
    trace = client.get(f"/api/traces/{started['trace_id']}", headers=bearer(owner)).json()
    assert [event["stage"] for event in trace["events"]].count("shadow_started") == 1
    assert [event["stage"] for event in trace["events"]].count("shadow_completed") == 1
    assert trace["status"] == "completed"


def test_rejected_approval_never_emits_shadow_and_trace_payload_is_safe(client):
    owner = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
    approver = issue_token("TENANT-A", "APPROVER-A", Role.APPROVER)
    started = _start_refund(client, owner, "REQ-TRACE-REJECT")

    decision = client.post(
        f"/api/approvals/{started['approval_id']}/decision",
        headers=bearer(approver),
        json={"approved": False, "confirmation": False},
    )
    assert decision.status_code == 200
    trace_response = client.get(f"/api/traces/{started['trace_id']}", headers=bearer(owner))
    assert trace_response.status_code == 200
    trace = trace_response.json()
    stages = [event["stage"] for event in trace["events"]]
    assert "shadow_started" not in stages
    assert "shadow_completed" not in stages
    assert trace["status"] == "rejected"

    serialized = trace_response.text.lower()
    for forbidden in ("tenant_id", "user_id", "prompt", "messages", "address", "phone"):
        assert forbidden not in serialized
    # customer 只能看到安全生命周期，不看到内部节点/工具标识。
    assert all(event.get("name") is None and event.get("tool") is None for event in trace["events"])
