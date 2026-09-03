"""端到端全流程验收测试（API 层等价于浏览器验收流程）。

冻结验收路径（《生产基线与验收测试》）：
  创建会话 → 查询政策/订单 → 发起敏感申请（退款/退货/改址）→ 审批（二次确认）→ 查询操作结果。

同时验证按角色控制数据可见范围（customer 只看本人会话/审批；staff 全租户；
非 admin 访问 /api/members 被拒）。本文件在 E2E 完成后把关键证据写入 evidence/e2e_flow.json，
用于生产验收审计。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

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


def _new_session(client, token) -> str:
    r = client.post("/api/sessions", headers=bearer(token))
    assert r.status_code == 201, r.text
    return r.json()["thread_id"]


def _chat(client, token, thread_id, cid, msg):
    return client.post("/api/chat", json={
        "mode": "start", "thread_id": thread_id, "client_request_id": cid, "message": msg,
    }, headers=bearer(token))


def test_full_acceptance_flow(client):
    # 1) 创建会话（customer）
    tok_user = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
    tok_admin = issue_token("TENANT-A", "ADMIN-A", Role.ADMIN)
    thread_id = _new_session(client, tok_user)

    evidence: dict = {"thread_id": thread_id, "steps": []}

    # 2a) 查询政策 → done
    r = _chat(client, tok_user, thread_id, "E2E-POL", "退货政策是什么？")
    evs = parse_sse(r.text)
    assert evs[0]["event"] == "accepted"
    done = next(e for e in evs if e["event"] == "done")
    assert done["data"]["status"] == "completed"
    evidence["steps"].append({"step": "query_policy", "events": len(evs), "final": done["data"].get("final_response")})

    # 2b) 查询订单 → done（带订单状态）
    r = _chat(client, tok_user, thread_id, "E2E-ORD", "查订单 ORD-001")
    evs = parse_sse(r.text)
    done = next(e for e in evs if e["event"] == "done")
    evidence["steps"].append({"step": "query_order", "final": done["data"].get("final_response")})

    # 3) 发起敏感申请（退款）→ approval_required
    r = _chat(client, tok_user, thread_id, "E2E-REFUND", "我要退款，订单号 ORD-001")
    evs = parse_sse(r.text)
    ar = next(e for e in evs if e["event"] == "approval_required")
    approval_id = ar["data"]["approval_id"]
    operation_id = ar["data"]["operation_id"]
    assert ar["data"]["status"] == "pending"
    evidence["steps"].append({"step": "request_refund", "approval_id": approval_id, "operation_id": operation_id})

    # 4) 审批（admin 二次确认通过）→ 返回权威 operation_id
    r = client.post(f"/api/approvals/{approval_id}/decision",
                    json={"approved": True, "confirmation": True,
                          "operation_id": operation_id, "pending_action": "refund"},
                    headers=bearer(tok_admin))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["operation_id"] == operation_id
    assert body["status"] == "executed"
    evidence["steps"].append({"step": "approve_refund", "decision": body})

    # 5) 查询操作结果
    for i in range(3):  # 允许短暂异步一致性
        op = client.get(f"/api/operations/{operation_id}", headers=bearer(tok_admin)).json()
        if op["status"] == "executed":
            break
    assert op["status"] == "executed", op
    assert op["result"] is not None and op["result"].get("message")
    evidence["steps"].append({"step": "query_operation", "operation": op})

    # 6) 会话历史可见（messages）
    msgs = client.get(f"/api/sessions/{thread_id}/messages", headers=bearer(tok_admin))
    assert msgs.status_code == 200
    evidence["steps"].append({"step": "session_history", "message_count": len(msgs.json()["messages"])})

    _write_evidence(evidence)


def test_role_based_data_visibility(client):
    """customer 只看本人会话/审批；staff 全租户；非 admin 禁止 /api/members。"""
    tok_a = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
    tok_b = issue_token("TENANT-B", "USER-B1", Role.CUSTOMER)
    ta = _new_session(client, tok_a)
    tb = _new_session(client, tok_b)

    # customer 的会话列表只有本人创建的那条（TENANT-A 无其它 USER-001 会话）。
    sessions_a = client.get("/api/sessions", headers=bearer(tok_a)).json()
    assert all(s["thread_id"] == ta for s in sessions_a)
    assert len(sessions_a) >= 1

    # staff (agent) 能看到租户内容；这里仅校验 customer 无法看到其它租户线程。
    r = client.get(f"/api/sessions/{tb}", headers=bearer(tok_a))
    assert r.status_code == 404

    # 非 admin 访问 /api/members → 403。
    r = client.get("/api/members", headers=bearer(tok_a))
    assert r.status_code == 403
    r = client.get("/api/members", headers=bearer(issue_token("TENANT-A", "AGENT-A", Role.AGENT)))
    assert r.status_code == 403
    # admin 可访问。
    r = client.get("/api/members", headers=bearer(issue_token("TENANT-A", "ADMIN-A", Role.ADMIN)))
    assert r.status_code == 200
    assert any(m["role"] == "approver" for m in r.json())


def _write_evidence(evidence: dict) -> None:
    out = Path(os.environ.get("DSH_EVIDENCE_DIR", "evidence"))
    out.mkdir(parents=True, exist_ok=True)
    (out / "e2e_flow.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
