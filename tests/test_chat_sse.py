"""SSE 契约与聊天流测试。

验证冻结契约：POST /api/chat 返回 text/event-stream；事件仅限六种；start 去重；
start/resume 字段约束；政策查询产生 done；退款产生 approval_required。
"""
from __future__ import annotations

import json

from tests.conftest import bearer
from src.auth.security import issue_token
from src.core.types import Role


def parse_sse(text: str) -> list[dict]:
    frames = []
    for block in text.strip().split("\n\n"):
        block = block.strip()
        if not block:
            continue
        d: dict = {}
        for line in block.split("\n"):
            if line.startswith("id: "):
                d["id"] = line[4:]
            elif line.startswith("event: "):
                d["event"] = line[7:]
            elif line.startswith("data: "):
                d["data"] = json.loads(line[6:])
        if "event" in d:
            frames.append(d)
    return frames


def _make_session(client, token) -> str:
    r = client.post("/api/sessions", headers=bearer(token))
    assert r.status_code == 201, r.text
    return r.json()["thread_id"]


def test_policy_query_sse(client):
    token = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
    thread_id = _make_session(client, token)
    resp = client.post("/api/chat", json={
        "mode": "start", "thread_id": thread_id,
        "client_request_id": "REQ-P1", "message": "退货政策是什么？",
    }, headers=bearer(token))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(resp.text)
    assert events[0]["event"] == "accepted"
    names = [e["event"] for e in events]
    assert "done" in names
    assert "approval_required" not in names
    # 每帧 id 严格递增
    ids = [int(e["id"]) for e in events]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)


def test_refund_generates_approval_required(client):
    token = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
    thread_id = _make_session(client, token)
    resp = client.post("/api/chat", json={
        "mode": "start", "thread_id": thread_id,
        "client_request_id": "REQ-R1", "message": "我要退款，订单号 ORD-001",
    }, headers=bearer(token))
    events = parse_sse(resp.text)
    names = [e["event"] for e in events]
    assert "approval_required" in names
    ar = next(e for e in events if e["event"] == "approval_required")
    assert "approval_id" in ar["data"] and "operation_id" in ar["data"]


def test_start_body_rejects_unknown_tenant_field(client):
    token = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
    thread_id = _make_session(client, token)
    # 客户端试图覆盖 tenant_id → 必须被 schema/入口拒绝。
    resp = client.post("/api/chat", json={
        "mode": "start", "thread_id": thread_id,
        "client_request_id": "REQ-X1", "message": "你好", "tenant_id": "TENANT-B",
    }, headers=bearer(token))
    assert resp.status_code == 422


def test_resume_requires_last_event_id(client):
    token = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
    thread_id = _make_session(client, token)
    start = client.post("/api/chat", json={
        "mode": "start", "thread_id": thread_id,
        "client_request_id": "REQ-R2", "message": "我要退款，订单号 ORD-001",
    }, headers=bearer(token))
    events = parse_sse(start.text)
    stream_id = events[0]["data"]["stream_id"]
    # 无 Last-Event-ID → 422
    r = client.post("/api/chat", json={"mode": "resume", "stream_id": stream_id}, headers=bearer(token))
    assert r.status_code == 422


def test_start_is_idempotent_by_client_request_id(client):
    token = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
    thread_id = _make_session(client, token)
    payload = {"mode": "start", "thread_id": thread_id,
               "client_request_id": "REQ-R3", "message": "我要退款，订单号 ORD-001"}
    r1 = client.post("/api/chat", json=payload, headers=bearer(token))
    e1 = parse_sse(r1.text)
    # 重投同一 client_request_id → 只重放既有流，不新建第二个图执行/敏感操作。
    r2 = client.post("/api/chat", json=payload, headers=bearer(token))
    e2 = parse_sse(r2.text)
    assert e1[0]["event"] == "accepted" and e2[0]["event"] == "accepted"
    ar1 = [e for e in e1 if e["event"] == "approval_required"]
    ar2 = [e for e in e2 if e["event"] == "approval_required"]
    assert len(ar1) == 1 and len(ar2) == 1
    assert ar1[0]["data"]["operation_id"] == ar2[0]["data"]["operation_id"]
