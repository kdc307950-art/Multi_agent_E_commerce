"""Live HTTP 冒烟：对运行中的后端执行创建会话 → 查询政策 → 退款 → 审批 → 查操作 全流程。

可指定 BASE 以绕过前端反向代理（浏览器→前端→后端链路）验证。
用法：python scripts/live_e2e.py [BASE] [OUTFILE]
"""
import json
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000/api"
OUTFILE = sys.argv[2] if len(sys.argv) > 2 else "evidence/http_e2e.json"
AUTH = "Bearer mock:TENANT-A:USER-001:customer:deadbeef1234"
AUTH_ADMIN = "Bearer mock:TENANT-A:ADMIN-A:admin:deadbeef5678"


def req(method, path, body=None, auth=None, accept=None, last_event_id=None):
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = auth
    if accept:
        headers["Accept"] = accept
    if last_event_id:
        headers["Last-Event-ID"] = str(last_event_id)
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(r) as resp:
        return resp.status, resp.headers, resp.read().decode()


def parse_sse(text):
    frames = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        d = {}
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


out = {"steps": []}
st, _, body = req("POST", "/sessions", {}, auth=AUTH)
tid = json.loads(body)["thread_id"]
out["steps"].append({"step": "create_session", "thread_id": tid})

st, _, body = req("POST", "/chat",
                  {"mode": "start", "thread_id": tid, "client_request_id": "LIVE-1", "message": "退货政策是什么？"},
                  auth=AUTH)
evs = parse_sse(body)
done = next(e for e in evs if e["event"] == "done")
out["steps"].append({"step": "policy", "events": len(evs), "final": done["data"].get("final_response")})

st, _, body = req("POST", "/chat",
                  {"mode": "start", "thread_id": tid, "client_request_id": "LIVE-2", "message": "我要退款，订单号 ORD-001"},
                  auth=AUTH)
evs = parse_sse(body)
ar = next(e for e in evs if e["event"] == "approval_required")
aid, oid = ar["data"]["approval_id"], ar["data"]["operation_id"]
out["steps"].append({"step": "request_refund", "approval_id": aid, "operation_id": oid})

st, _, body = req("POST", f"/approvals/{aid}/decision",
                  {"approved": True, "confirmation": True, "operation_id": oid, "pending_action": "refund"},
                  auth=AUTH_ADMIN)
out["steps"].append({"step": "approve", "decision": json.loads(body)})

st, _, body = req("GET", f"/operations/{oid}", auth=AUTH_ADMIN)
op = json.loads(body)
out["steps"].append({"step": "query_operation", "operation": op})

assert op["status"] == "executed", op
print(json.dumps(out, ensure_ascii=False, indent=2))
with open(OUTFILE, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print("OK ->", OUTFILE)
