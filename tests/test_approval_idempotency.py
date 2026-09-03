"""审批幂等与无绕过测试。

验证冻结契约：
- 退款/退货/改址必须人工审批；无 direct -> execute 绕过。
- 审批通过后进入 execute_*，生成唯一 operation_id；重复提交不重复执行。
- 拒绝后不执行。
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from tests.conftest import bearer
from tests.test_chat_sse import parse_sse as _parse
from src.auth.security import issue_token
from src.core.types import (
    ApprovalStatus,
    ErrorCode,
    OperationStatus,
    PendingAction,
    Role,
    generate_operation_key,
)
from src.graph.nodes import make_nodes


def test_refund_requires_approval_then_execute(client):
    token_user = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
    token_admin = issue_token("TENANT-A", "ADMIN-A", Role.ADMIN)
    r = client.post("/api/sessions", headers=bearer(token_user))
    thread_id = r.json()["thread_id"]

    resp = client.post("/api/chat", json={
        "mode": "start", "thread_id": thread_id,
        "client_request_id": "REQ-IDEM1", "message": "我要退款，订单号 ORD-001",
    }, headers=bearer(token_user))
    ar = next(e for e in _parse(resp.text) if e["event"] == "approval_required")
    approval_id = ar["data"]["approval_id"]
    operation_id = ar["data"]["operation_id"]

    # 审批前：操作必须处于 pending（未执行），证明无 direct 绕过。
    before = client.get(f"/api/operations/{operation_id}", headers=bearer(token_admin))
    assert before.json()["status"] == "pending"

    # 非审批角色不能审批。
    resp_deny = client.post(f"/api/approvals/{approval_id}/decision",
                            json={"approved": True, "confirmation": True}, headers=bearer(token_user))
    assert resp_deny.status_code == 403

    # 缺二次确认 → 422。
    resp_no_conf = client.post(f"/api/approvals/{approval_id}/decision",
                               json={"approved": True, "confirmation": False}, headers=bearer(token_admin))
    assert resp_no_conf.status_code == 422

    # 审批通过。
    resp_ok = client.post(f"/api/approvals/{approval_id}/decision",
                          json={"approved": True, "confirmation": True}, headers=bearer(token_admin))
    assert resp_ok.status_code == 200
    assert resp_ok.json()["operation_id"] == operation_id

    after = client.get(f"/api/operations/{operation_id}", headers=bearer(token_admin))
    assert after.json()["status"] == "executed"


def test_approval_decision_is_idempotent(client):
    token_user = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
    token_admin = issue_token("TENANT-A", "ADMIN-A", Role.ADMIN)
    thread_id = client.post("/api/sessions", headers=bearer(token_user)).json()["thread_id"]
    resp = client.post("/api/chat", json={
        "mode": "start", "thread_id": thread_id,
        "client_request_id": "REQ-IDEM2", "message": "我要退货，订单号 ORD-001",
    }, headers=bearer(token_user))
    ar = next(e for e in _parse(resp.text) if e["event"] == "approval_required")
    approval_id, operation_id = ar["data"]["approval_id"], ar["data"]["operation_id"]

    # 第一次审批。
    r1 = client.post(f"/api/approvals/{approval_id}/decision",
                     json={"approved": True, "confirmation": True}, headers=bearer(token_admin))
    assert r1.json()["operation_id"] == operation_id

    # 重复提交同一审批 → 幂等重放，同一 operation_id，不重复执行。
    r2 = client.post(f"/api/approvals/{approval_id}/decision",
                     json={"approved": True, "confirmation": True}, headers=bearer(token_admin))
    assert r2.json()["operation_id"] == operation_id
    assert r2.json()["status"] == "executed"
    op = client.get(f"/api/operations/{operation_id}", headers=bearer(token_admin)).json()
    assert op["status"] == "executed"


def test_rejected_approval_does_not_execute(client):
    token_user = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
    token_admin = issue_token("TENANT-A", "ADMIN-A", Role.ADMIN)
    thread_id = client.post("/api/sessions", headers=bearer(token_user)).json()["thread_id"]
    resp = client.post("/api/chat", json={
        "mode": "start", "thread_id": thread_id,
        "client_request_id": "REQ-IDEM3", "message": "我要退款，订单号 ORD-001",
    }, headers=bearer(token_user))
    ar = next(e for e in _parse(resp.text) if e["event"] == "approval_required")
    approval_id, operation_id = ar["data"]["approval_id"], ar["data"]["operation_id"]

    r = client.post(f"/api/approvals/{approval_id}/decision",
                    json={"approved": False, "feedback": "信息存疑"}, headers=bearer(token_admin))
    assert r.status_code == 200
    op = client.get(f"/api/operations/{operation_id}", headers=bearer(token_admin)).json()
    # 拒绝后不执行。
    assert op["status"] != "executed"


def test_generated_operation_id_is_unique_per_tenant(client, store):
    # 两个租户同名订单/请求各自产生独立 operation_id（联合唯一键隔离的体现）。
    token_a = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
    token_b = issue_token("TENANT-B", "USER-B1", Role.CUSTOMER)
    ta = client.post("/api/sessions", headers=bearer(token_a)).json()["thread_id"]
    tb = client.post("/api/sessions", headers=bearer(token_b)).json()["thread_id"]
    ra = client.post("/api/chat", json={"mode": "start", "thread_id": ta,
                                        "client_request_id": "REQ-SA1", "message": "我要退款，订单号 ORD-001"},
                     headers=bearer(token_a))
    rb = client.post("/api/chat", json={"mode": "start", "thread_id": tb,
                                        "client_request_id": "REQ-SB1", "message": "我要退货，订单号 ORD-001"},
                     headers=bearer(token_b))
    oa = next(e for e in _parse(ra.text) if e["event"] == "approval_required")["data"]["operation_id"]
    ob = next(e for e in _parse(rb.text) if e["event"] == "approval_required")["data"]["operation_id"]
    assert oa != ob


# ---------------------------------------------------------------------------
# 审批安全强化：绑定决策、CAS 防并发双执行、拒绝联动、超时恢复、残留清理
# ---------------------------------------------------------------------------
def _make_pending_refund(store, thread="th-x", order="ORD-001", rid="REQ-X"):
    op = store.create_operation("TENANT-A", thread, order, PendingAction.REFUND,
                                generate_operation_key(PendingAction.REFUND, "TENANT-A", order, rid),
                                1.0)
    appr = store.create_approval("TENANT-A", thread, op.operation_id, PendingAction.REFUND,
                                 order, 100.0, "申请退款", 1.0)
    return op, appr


def test_concurrent_decision_only_one_claims(store):
    """并发审批只允许一个抢占成功（CAS），其余重放不重复生效。"""
    op, appr = _make_pending_refund(store, thread="th-c", rid="REQ-CAS")
    barrier = threading.Barrier(8)
    results: dict = {}

    def claim(approver: str) -> None:
        barrier.wait()
        a, claimed = store.claim_approval_decision(
            "TENANT-A", appr.approval_id, approver, True, "ok", 2.0)
        results[approver] = (a.status.value, claimed)

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(claim, [f"ADMIN-{i}" for i in range(8)]))
    claimed_approvers = [k for k, (_s, c) in results.items() if c]
    assert len(claimed_approvers) == 1  # 仅一个真正抢占成功
    final = store.get_approval("TENANT-A", appr.approval_id)
    assert final.status == ApprovalStatus.APPROVED
    assert final.approver == claimed_approvers[0]
    assert store.get_operation("TENANT-A", op.operation_id).status == OperationStatus.PENDING


def test_reject_marks_operation_rejected(store):
    """拒绝后 operation 置 rejected，不执行。"""
    op, appr = _make_pending_refund(store, thread="th-r", rid="REQ-RJ")
    store.claim_approval_decision("TENANT-A", appr.approval_id, "ADMIN-A", False, "信息不符", 2.0)
    assert store.get_approval("TENANT-A", appr.approval_id).status == ApprovalStatus.REJECTED
    assert store.get_operation("TENANT-A", op.operation_id).status == OperationStatus.REJECTED


def test_reject_with_handoff_feedback_marks_human_handoff(store):
    """拒绝反馈要求转人工时，operation 置 human_handoff。"""
    op, appr = _make_pending_refund(store, thread="th-h", rid="REQ-HO")
    store.claim_approval_decision("TENANT-A", appr.approval_id, "ADMIN-A", False, "请转人工核对", 2.0)
    assert store.get_operation("TENANT-A", op.operation_id).status == OperationStatus.HUMAN_HANDOFF


def test_stale_approval_expires_to_timeout_and_handoff(store):
    """审批超时：标记 timeout 并把 operation 转人工，保留 operation_id。"""
    op, appr = _make_pending_refund(store, thread="th-t", rid="REQ-TO")
    expired = store.expire_stale_approvals("TENANT-A", now=10.0, timeout_seconds=5.0)
    assert appr.approval_id in expired
    assert store.get_approval("TENANT-A", appr.approval_id).status == ApprovalStatus.TIMEOUT
    assert store.get_operation("TENANT-A", op.operation_id).status == OperationStatus.HUMAN_HANDOFF


def test_decision_binds_operation_id_and_action(client):
    """审批决定必须与 approval 绑定的 operation_id / pending_action 一致。"""
    token_user = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
    token_admin = issue_token("TENANT-A", "ADMIN-A", Role.ADMIN)
    thread_id = client.post("/api/sessions", headers=bearer(token_user)).json()["thread_id"]
    resp = client.post("/api/chat", json={
        "mode": "start", "thread_id": thread_id,
        "client_request_id": "REQ-BIND1", "message": "我要退款，订单号 ORD-001",
    }, headers=bearer(token_user))
    ar = next(e for e in _parse(resp.text) if e["event"] == "approval_required")
    approval_id, operation_id = ar["data"]["approval_id"], ar["data"]["operation_id"]

    r_bad_op = client.post(f"/api/approvals/{approval_id}/decision",
                           json={"approved": True, "confirmation": True,
                                 "operation_id": "op-bogus"}, headers=bearer(token_admin))
    assert r_bad_op.status_code == 422
    r_bad_act = client.post(f"/api/approvals/{approval_id}/decision",
                            json={"approved": True, "confirmation": True,
                                  "pending_action": "return_request"}, headers=bearer(token_admin))
    assert r_bad_act.status_code == 422
    r_ok = client.post(f"/api/approvals/{approval_id}/decision",
                       json={"approved": True, "confirmation": True,
                             "operation_id": operation_id, "pending_action": "refund"},
                       headers=bearer(token_admin))
    assert r_ok.status_code == 200


def test_reject_then_reissue_new_operation(client):
    """拒绝后同 thread 再次发起生成新 operation，原 operation 不受影响。"""
    token_user = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
    token_admin = issue_token("TENANT-A", "ADMIN-A", Role.ADMIN)
    thread_id = client.post("/api/sessions", headers=bearer(token_user)).json()["thread_id"]
    r1 = client.post("/api/chat", json={
        "mode": "start", "thread_id": thread_id,
        "client_request_id": "REQ-REJ1", "message": "我要退款，订单号 ORD-001",
    }, headers=bearer(token_user))
    ar1 = next(e for e in _parse(r1.text) if e["event"] == "approval_required")
    aid1, opid1 = ar1["data"]["approval_id"], ar1["data"]["operation_id"]
    rd = client.post(f"/api/approvals/{aid1}/decision",
                     json={"approved": False, "feedback": "信息不符"}, headers=bearer(token_admin))
    assert rd.status_code == 200
    assert client.get(f"/api/operations/{opid1}", headers=bearer(token_admin)).json()["status"] == "rejected"

    r2 = client.post("/api/chat", json={
        "mode": "start", "thread_id": thread_id,
        "client_request_id": "REQ-REJ2", "message": "我要退款，订单号 ORD-001",
    }, headers=bearer(token_user))
    opid2 = next(e for e in _parse(r2.text) if e["event"] == "approval_required")["data"]["operation_id"]
    assert opid2 != opid1
    assert client.get(f"/api/operations/{opid2}", headers=bearer(token_admin)).json()["status"] == "pending"
    assert client.get(f"/api/operations/{opid1}", headers=bearer(token_admin)).json()["status"] == "rejected"


def test_execute_clears_residual_and_requires_approved(llm, store):
    """执行节点：审批通过才执行；执行后清理 needs_approval / pending_action 残留。

    user_id/role 必须来自服务端注入的 TenantContext（执行面要求），此处模拟齐全的认证状态。
    """
    op, appr = _make_pending_refund(store, thread="th-ex", rid="REQ-EX1")
    store.claim_approval_decision("TENANT-A", appr.approval_id, "ADMIN-A", True, None, 2.0)
    nodes = make_nodes(llm, store)
    out = nodes["execute_refund"]({
        "tenant_id": "TENANT-A", "user_id": "USER-001", "role": "customer",
        "operation_id": op.operation_id,
        "thread_id": "th-ex", "order_id": "ORD-001",
        "approval_id": appr.approval_id, "model": llm.model,
    })
    assert "执行完成" in out["final_response"]
    assert out["needs_approval"] is False
    assert out["pending_action"] is None
    assert store.get_operation("TENANT-A", op.operation_id).status == OperationStatus.EXECUTED


def test_execute_rejects_when_not_approved(llm, store):
    """执行节点复核审批状态：未 approved 一律拒绝执行。"""
    op, appr = _make_pending_refund(store, thread="th-na", rid="REQ-EX2")
    nodes = make_nodes(llm, store)
    out = nodes["execute_refund"]({
        "tenant_id": "TENANT-A", "operation_id": op.operation_id,
        "thread_id": "th-na", "order_id": "ORD-001",
        "approval_id": appr.approval_id, "model": llm.model,
    })
    assert out["error"] == ErrorCode.APPROVAL_BINDING_MISMATCH.value
    assert store.get_operation("TENANT-A", op.operation_id).status != OperationStatus.EXECUTED


def test_execute_rejects_action_mismatch(llm, store):
    """执行节点复核动作类型：refund 操作不允许送到 execute_return。"""
    op, _appr = _make_pending_refund(store, thread="th-am", rid="REQ-EX3")
    nodes = make_nodes(llm, store)
    out = nodes["execute_return"]({
        "tenant_id": "TENANT-A", "operation_id": op.operation_id,
        "thread_id": "th-am", "order_id": "ORD-001", "model": llm.model,
    })
    assert out["error"] == ErrorCode.APPROVAL_BINDING_MISMATCH.value
    assert store.get_operation("TENANT-A", op.operation_id).status != OperationStatus.EXECUTED


def test_execute_rejects_model_not_in_whitelist(llm, store):
    """执行节点复核模型白名单：低档模型不得执行写库。"""
    op, appr = _make_pending_refund(store, thread="th-mw", rid="REQ-EX4")
    store.claim_approval_decision("TENANT-A", appr.approval_id, "ADMIN-A", True, None, 2.0)
    nodes = make_nodes(llm, store)
    out = nodes["execute_refund"]({
        "tenant_id": "TENANT-A", "operation_id": op.operation_id,
        "thread_id": "th-mw", "order_id": "ORD-001",
        "approval_id": appr.approval_id, "model": "low-cost-model",
    })
    assert out["error"] == ErrorCode.MODEL_NOT_IN_WHITELIST.value
    assert store.get_operation("TENANT-A", op.operation_id).status != OperationStatus.EXECUTED


def test_same_thread_query_after_approval(client):
    """审批通过后同 thread 可继续后续查询，不触发新审批且原操作保持 executed。"""
    token_user = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
    token_admin = issue_token("TENANT-A", "ADMIN-A", Role.ADMIN)
    thread_id = client.post("/api/sessions", headers=bearer(token_user)).json()["thread_id"]
    r1 = client.post("/api/chat", json={
        "mode": "start", "thread_id": thread_id,
        "client_request_id": "REQ-QA", "message": "我要退款，订单号 ORD-001",
    }, headers=bearer(token_user))
    ar = next(e for e in _parse(r1.text) if e["event"] == "approval_required")
    aid, opid = ar["data"]["approval_id"], ar["data"]["operation_id"]
    r_dec = client.post(f"/api/approvals/{aid}/decision",
                        json={"approved": True, "confirmation": True}, headers=bearer(token_admin))
    assert r_dec.status_code == 200

    r2 = client.post("/api/chat", json={
        "mode": "start", "thread_id": thread_id,
        "client_request_id": "REQ-QB", "message": "查一下订单 ORD-001",
    }, headers=bearer(token_user))
    names = [e["event"] for e in _parse(r2.text)]
    assert "approval_required" not in names
    assert "done" in names
    assert client.get(f"/api/operations/{opid}", headers=bearer(token_admin)).json()["status"] == "executed"


def test_resume_replays_events_without_re_execution(client):
    """响应丢失后 resume：重放既有流事件，不重跑图/敏感写，避免重复业务副作用。"""
    token_user = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
    token_admin = issue_token("TENANT-A", "ADMIN-A", Role.ADMIN)
    thread_id = client.post("/api/sessions", headers=bearer(token_user)).json()["thread_id"]
    r1 = client.post("/api/chat", json={
        "mode": "start", "thread_id": thread_id,
        "client_request_id": "REQ-RES", "message": "我要退款，订单号 ORD-001",
    }, headers=bearer(token_user))
    events = _parse(r1.text)
    stream_id = events[0]["data"]["stream_id"]
    ar = next(e for e in events if e["event"] == "approval_required")
    aid, opid = ar["data"]["approval_id"], ar["data"]["operation_id"]
    client.post(f"/api/approvals/{aid}/decision",
                json={"approved": True, "confirmation": True}, headers=bearer(token_admin))

    r2 = client.post("/api/chat", json={"mode": "resume", "stream_id": stream_id},
                     headers={**bearer(token_user), "Last-Event-ID": "0"})
    assert r2.status_code == 200
    replay = _parse(r2.text)
    assert [e["event"] for e in replay]  # 重放到至少一个事件
    # 关键：resume 不产生新的审批决策/重复写，原操作仍 executed。
    assert client.get(f"/api/operations/{opid}", headers=bearer(token_admin)).json()["status"] == "executed"
