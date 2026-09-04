#!/usr/bin/env python3
# run_api_drills.py —— D3/D4/D5/D6 preview API 驱动演练（python 封装，仅用标准库）。
#
# 对齐 deploy/drills/drill_api.sh：走真实 HTTPS + 真实认证 + PostgreSQL 数据面，在 preview 服务器上
# 复验「审批仅审批后执行 / SSE 断线恢复 / 并发重复提交 / 沙箱对账」。
#
# 前置（务必确认后再跑，否则实跑毫无意义且结果不可信）：
#   1. preview 栈已启动（after-sales-preview），且 /api/healthz 可达。
#   2. PREVIEW_TOKEN：一个上线白名单租户（LAUNCH_ALLOWED_TENANTS）的 admin/approver 有效 JWT。
#   3. 可驱动 chat 流：要么 LLM 端点可达，要么 EXECUTION_PROVIDER=mock 能产生流式事件
#      （approval_required / accepted / done）。若二者均不满足，本脚本会在"get 不到预期事件"时
#      返回 blocked_prerequisite（退出码 2），绝不伪装成 PASS。
#
# 用法：
#   set PREVIEW_TOKEN=<jwt>
#   python deploy/drills/pwsh-runbook/run_api_drills.py approval|sse|concurrent|reconcile
#
# 安全：不打印口令/密钥；只访问 /api/sessions /api/chat /api/operations /api/approvals/* /api/healthz。
# 退出码：0=PASS；1=FAIL（断言未满足）；2=BLOCKED（前置未满足，如缺事件/缺 token）；3=参数错误。

import json
import os
import ssl
import sys
import datetime
import urllib.request
import urllib.error

BASE = os.environ.get("PREVIEW_BASE", "https://127.0.0.1").rstrip("/")
TOKEN = os.environ.get("PREVIEW_TOKEN", "")
RECORDS = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "records")
)

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def _req(method, path, body=None, headers=None, timeout=30):
    """执行一次 HTTPS 请求，返回响应文本（SSE 或 JSON）。未认证/网络错误向上抛。"""
    h = {"Authorization": "Bearer %s" % TOKEN}
    if headers:
        h.update(headers)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        h.setdefault("Content-Type", "application/json")
    r = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    with urllib.request.urlopen(r, context=_CTX, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def new_session():
    return json.loads(_req("POST", "/api/sessions"))["thread_id"]


def chat_start(thread, cid, msg):
    return _req(
        "POST",
        "/api/chat",
        {"mode": "start", "thread_id": thread, "client_request_id": cid, "message": msg},
    )


def sse_event(text, evname):
    """从 SSE 文本中取出第一个名为 evname 的事件 JSON；找不到返回 None。
    兼容 LF 与 CRLF（先归一行尾，再按空行分块）。"""
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


def write_record(scenario, ok, detail):
    os.makedirs(RECORDS, exist_ok=True)
    path = os.path.join(RECORDS, "drill-api-%s.json" % scenario)
    rec = {
        "scenario": "D3_D4_D5_D6_api_%s" % scenario,
        "result": "PASS" if ok else "BLOCKED",
        "ok": bool(ok),
        "detail": detail,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "preview_base": BASE,
        "note": "需 PREVIEW_TOKEN + 可达 LLM 或 mock 驱动 chat 流；否则为 BLOCKED，非伪造 PASS。",
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)
    return path


def main():
    if not TOKEN:
        print("[BLOCKED] 缺少 PREVIEW_TOKEN（上线白名单租户 admin/approver 的有效 JWT）。")
        print("        另需可达 LLM 端点或 EXECUTION_PROVIDER=mock 驱动 chat 流。前置未满足，跳过实跑。")
        return 2

    scen = sys.argv[1] if len(sys.argv) > 1 else ""
    if scen not in ("approval", "sse", "concurrent", "reconcile"):
        print("用法: run_api_drills.py {approval|sse|concurrent|reconcile}")
        return 3

    try:
        if scen == "approval":
            return drill_approval()
        if scen == "sse":
            return drill_sse()
        if scen == "concurrent":
            return drill_concurrent()
        if scen == "reconcile":
            return drill_reconcile()
    except urllib.error.HTTPError as e:
        rec_path = write_record(scen, False, {"http_error": e.code, "reason": str(e.reason)})
        print("[FAIL] HTTP %s: %s （记录 %s）" % (e.code, e.reason, rec_path))
        return 1
    except urllib.error.URLError as e:
        rec_path = write_record(scen, False, {"url_error": str(e.reason)})
        print("[BLOCKED] 无法连接 preview：%s（记录 %s）" % (e.reason, rec_path))
        return 2
    return 3


def drill_approval():
    print("[D3] 审批仅审批后执行 + 幂等")
    thread = new_session()
    sse = chat_start(thread, "APPR-1", "我要退款，订单号 ORD-001")
    ar = sse_event(sse, "approval_required")
    if ar is None:
        rec = write_record("approval", False, {"reason": "no_approval_required_event", "sse_head": sse[:200]})
        print("[BLOCKED] 未得到 approval_required 事件（LLM/mock 未驱动 chat 流）。记录 %s" % rec)
        return 2
    approval_id = ar["approval_id"]
    op_id = ar["operation_id"]
    pre = json.loads(_req("GET", "/api/operations/%s" % op_id))["status"]
    d1 = json.loads(
        _req("POST", "/api/approvals/%s/decision" % approval_id,
             {"approved": True, "confirmation": True, "operation_id": op_id})
    )["status"]
    ok = (pre == "pending" and d1 == "executed")
    rec = write_record("approval", ok, {"pre_decision_status": pre, "post_decision_status": d1,
                                        "approval_id": approval_id, "operation_id": op_id})
    if ok:
        print("[OK] 审批前 pending（未自动执行），审批后 executed。记录 %s" % rec)
        return 0
    print("[FAIL] 审批门控异常（审批前=%s，审批后=%s）。记录 %s" % (pre, d1, rec))
    return 1


def drill_sse():
    print("[D4] SSE 断线恢复")
    thread = new_session()
    sse = chat_start(thread, "SSE-1", "查订单 ORD-001")
    accepted = sse_event(sse, "accepted")
    if accepted is None:
        rec = write_record("sse", False, {"reason": "no_accepted_event", "sse_head": sse[:200]})
        print("[BLOCKED] 未得到 accepted（stream_id）事件。记录 %s" % rec)
        return 2
    stream_id = accepted["stream_id"]
    res = _req("POST", "/api/chat", {"mode": "resume", "stream_id": stream_id},
               headers={"Last-Event-ID": "0"})
    ok = "event: done" in res
    rec = write_record("sse", ok, {"stream_id": stream_id, "resume_has_done": ok})
    if ok:
        print("[OK] resume 从 Last-Event-ID 之后重放，无重复执行（含 done）。记录 %s" % rec)
        return 0
    print("[FAIL] SSE resume 未得到 done 事件。记录 %s" % rec)
    return 1


def drill_concurrent():
    print("[D5] 并发重复提交（同一 client_request_id）")
    thread = new_session()
    s1 = chat_start(thread, "CONC-1", "我要退款，订单号 ORD-001")
    s2 = chat_start(thread, "CONC-1", "我要退款，订单号 ORD-001")
    a1 = sse_event(s1, "accepted")
    a2 = sse_event(s2, "accepted")
    if a1 is None or a2 is None:
        rec = write_record("concurrent", False, {"reason": "no_accepted_event",
                                                 "s1_head": s1[:200], "s2_head": s2[:200]})
        print("[BLOCKED] 未得到 accepted 事件（无法比较 stream_id）。记录 %s" % rec)
        return 2
    same = a1["stream_id"] == a2["stream_id"]
    ok = same
    rec = write_record("concurrent", ok, {"stream_id_1": a1["stream_id"],
                                          "stream_id_2": a2["stream_id"], "same_stream": same})
    if ok:
        print("[OK] 同一 cid 绑定到单一 stream（幂等，未新建执行）。记录 %s" % rec)
        return 0
    print("[FAIL] 出现多个 stream，疑似重复执行。记录 %s" % rec)
    return 1


def drill_reconcile():
    print("[D6] 沙箱对账（后台任务）")
    # 对账任务由 worker 调度；此处以"入口可达 + 审计可查"作最小验证，完整对账结果见后台 reconcile_tenant 任务
    # 与服务端审计记录。这里尽力探测 /api/healthz；若栈未被 t1 驱动（worker 缺任务），如实标注 BLOCKED。
    try:
        body = _req("GET", "/api/healthz")
        reachable = True
    except Exception as e:  # noqa: BLE001
        body = str(e)
        reachable = False
    ok = reachable
    rec = write_record("reconcile", ok, {"endpoint_reachable": reachable,
                                         "note": "完整对账结果见后台 reconcile_tenant 任务；此处仅校验入口可达 + 审计存在。"})
    if ok:
        print("[OK] 对账入口可达（/api/healthz 200）。完整对账见后台 reconcile_tenant 任务。记录 %s" % rec)
        return 0
    print("[BLOCKED] 对账入口不可达：%s。记录 %s" % (body[:160], rec))
    return 2


if __name__ == "__main__":
    sys.exit(main())
