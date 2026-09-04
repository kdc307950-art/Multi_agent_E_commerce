#!/usr/bin/env python3
# verify_approval_security.py —— D3 关键证据补充：审批幂等收敛 + 跨租户 404 + 审批往返耗时。
#
# 覆盖：
#   A) 同一 approval_id 重复决策收敛到单一 operation_id（不重复执行、不新建 operation）。
#   B) 跨租户审批访问/决策返回 404（不泄露存在性）。
#   并记录审批往返耗时（approval_required → 首次决策 status=executed）。
#
# 用法：python deploy/drills/pwsh-runbook/verify_approval_security.py
# 前置：已启动 preview 栈；LLM_BACKEND=mock。明文登录口令由环境变量 PREVIEW_LOGIN_CREDENTIALS
#       （JSON 对象 {"<tenant>:<user>": "<明文>"}）或交互式 getpass 注入，**不读取明文文件、不硬编码**。
# 结果写入 deploy/drills/records/approval-security-extra.json。

import getpass
import json
import os
import ssl
import sys
import time
import datetime
import urllib.request
import urllib.error

BASE = os.environ.get("PREVIEW_BASE", "https://127.0.0.1").rstrip("/")
HERE = os.path.dirname(os.path.abspath(__file__))
RECORD_DIR = os.path.abspath(os.path.join(HERE, "..", "records"))

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


def load_plain() -> dict[str, str]:
    """读取明文登录口令表：优先环境变量 `PREVIEW_LOGIN_CREDENTIALS`（JSON 对象），
    否则对所需用户交互式 getpass（无回显、不落日志）。绝不读取明文文件或硬编码。"""
    raw = os.environ.get("PREVIEW_LOGIN_CREDENTIALS", "").strip()
    if raw:
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                return {str(k): str(v) for k, v in obj.items()}
        except Exception as exc:  # noqa: BLE001
            print("[警告] PREVIEW_LOGIN_CREDENTIALS 非法 JSON：%s" % exc, file=sys.stderr)
    creds: dict[str, str] = {}
    for user in ("TENANT-A:APPROVER-A", "TENANT-B:APPROVER-B"):
        try:
            creds[user] = getpass.getpass("请输入 %s 的明文口令（不落日志）：" % user)
        except EOFError:
            creds[user] = ""
    return creds


def login(creds, user):
    tid, uid = user.split(":", 1)
    body = json.dumps({"tenant_id": tid, "user_id": uid, "credential": creds[user]}).encode("utf-8")
    req = urllib.request.Request(BASE + "/api/auth/login", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, context=_ctx, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))["access_token"]


def req(token, method, path, body=None, headers=None):
    h = {"Authorization": "Bearer " + token}
    if headers:
        h.update(headers)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    if data is not None:
        h.setdefault("Content-Type", "application/json")
    r = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, context=_ctx, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def sse_event(text, evname):
    text = text.replace("\r\n", "\n")
    for blk in text.split("\n\n"):
        ev = None
        for line in blk.split("\n"):
            if line.startswith("event: "):
                ev = line[7:].strip()
            if line.startswith("data: "):
                d = json.loads(line[6:])
                if ev == evname:
                    return d
    return None


def new_session(token):
    st, body = req(token, "POST", "/api/sessions")
    return json.loads(body)["thread_id"]


def chat(token, thread, cid, msg):
    st, body = req(token, "POST", "/api/chat",
                   {"mode": "start", "thread_id": thread, "client_request_id": cid, "message": msg})
    return body


def approval_flow(token, cid):
    """建会话→chat 退款→取 approval_required 的 (approval_id, operation_id)。"""
    thread = new_session(token)
    sse = chat(token, thread, cid, "我要退款，订单号 ORD-001")
    ar = sse_event(sse, "approval_required")
    if ar is None:
        return None, None, None
    return thread, ar["approval_id"], ar["operation_id"]


def main():
    creds = load_plain()
    tokA = login(creds, "TENANT-A:APPROVER-A")
    tokB = login(creds, "TENANT-B:APPROVER-B")
    results = {}

    # ---- Test A: 同一 approval_id 重复决策收敛单一 operation_id + 审批往返耗时 ----
    threadA, aid, opid = approval_flow(tokA, "APPR-REP-1")
    if aid is None:
        results["repeated_decision"] = {"result": "BLOCKED", "detail": {"reason": "no_approval_required_event"}}
    else:
        st0, b0 = req(tokA, "GET", "/api/operations/" + opid)
        pre_status = json.loads(b0).get("status")
        t0 = time.time()
        st1, b1 = req(tokA, "POST", "/api/approvals/%s/decision" % aid,
                      {"approved": True, "confirmation": True, "operation_id": opid})
        d1 = json.loads(b1)
        # 重复决策（同一 approval_id + 同一 operation_id）
        st2, b2 = req(tokA, "POST", "/api/approvals/%s/decision" % aid,
                      {"approved": True, "confirmation": True, "operation_id": opid})
        d2 = json.loads(b2)
        st3, b3 = req(tokA, "GET", "/api/operations/" + opid)
        post_status = json.loads(b3).get("status")
        rt_s = round(time.time() - t0, 3)
        ok = (pre_status == "pending" and post_status == "executed"
              and d2.get("operation_id") == opid and d2.get("status") == "executed")
        results["repeated_decision"] = {
            "result": "PASS" if ok else "FAIL",
            "detail": {
                "approval_id": aid, "operation_id": opid,
                "pre_decision_status": pre_status,
                "first_decision_status": d1.get("status"),
                "first_decision_operation_id": d1.get("operation_id"),
                "second_decision_status": d2.get("status"),
                "second_decision_operation_id": d2.get("operation_id"),
                "post_status": post_status,
                "approve_round_trip_s": rt_s,
                "converged_single_operation": (d2.get("operation_id") == opid),
            },
        }

    # ---- Test B: 跨租户审批 404 ----
    threadB, aidB, opidB = approval_flow(tokA, "APPR-XT-1")
    if aidB is None:
        results["cross_tenant_404"] = {"result": "BLOCKED", "detail": {"reason": "no_approval_required_event"}}
    else:
        st_read, body_read = req(tokB, "GET", "/api/approvals/" + aidB)
        st_dec, body_dec = req(tokB, "POST", "/api/approvals/%s/decision" % aidB,
                               {"approved": True, "confirmation": True, "operation_id": opidB})
        ok = (st_read == 404 and st_dec == 404)
        results["cross_tenant_404"] = {
            "result": "PASS" if ok else "FAIL",
            "detail": {"cross_tenant_get_status": st_read, "cross_tenant_decision_status": st_dec},
        }

    os.makedirs(RECORD_DIR, exist_ok=True)
    rec = {
        "scenario": "approval_security_extra",
        "results": results,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "preview_base": BASE,
    }
    path = os.path.join(RECORD_DIR, "approval-security-extra.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)
    print(json.dumps({k: v["result"] for k, v in results.items()}, ensure_ascii=False))
    print("record:", path)
    return 0 if all(v["result"] == "PASS" for v in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
