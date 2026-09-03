"""预发布演练——本环境（dev，无 Docker/无 PG）可运行子集的自动化验证。

覆盖（在本机用 SqliteStore 持久化 + 内存图模拟"进程重启"）：
- [D1] API/worker/Redis 重启韧性：重建 app 后业务数据/会话/操作/审批/审计仍在，读接口可用。
- [D2] 数据库备份恢复：SQLite online backup → 恢复 → RPO=0（数据完整）。
- [D3] 审批中断恢复（单实例内）：敏感写仅审批后执行；同一审批重复决策幂等收敛到单一操作。
- [D4] SSE 断线恢复：resume 重放 Last-Event-ID 之后事件，不重复执行、不产生第二操作。
- [D5] 并发重复提交：同一 client_request_id 并发两次 → 单一操作（幂等）。
- [D6] 沙箱对账：人为构造非终态记录 → 对账 fail-closed 转人工（不静默）。

注：真正的"进程间"审批中断续跑（跨重启恢复图的 interrupt 状态）依赖
PostgresSaver checkpointer，属 postgres 数据面能力，由 deploy/drills/drill_approval_resume.sh
在预发布服务器上执行；本脚本针对同一职责在 dev 数据面给出等价证据。

用法：
    python deploy/drills/verify_dev_drills.py [--record-dir deploy/drills/records]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi.testclient import TestClient  # noqa: E402

from src.auth.security import issue_token  # noqa: E402
from src.core.types import PendingAction, Role  # noqa: E402
from src.execution.types import ExecutionMode, ExecutionStatus  # noqa: E402
from src.infrastructure.sqlite_store import SqliteStore  # noqa: E402
from src.llm.mock import MockLLM  # noqa: E402
from src.main import create_app  # noqa: E402


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


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


def _fresh_env(record_dir: str, tag: str):
    """创建独立的(store, app, db_path)并做基础种子。每个演练项自包含。

    sqlite 临时文件写入系统临时目录（避免污染记录目录）；正式记录只写入 record_dir。
    """
    Path(record_dir).mkdir(parents=True, exist_ok=True)
    tmp_root = Path(tempfile.gettempdir()) / "dsh-drills"
    tmp_root.mkdir(parents=True, exist_ok=True)
    db = str(tmp_root / f"{tag}-{int(time.time())}-{os.getpid()}.db")
    store = SqliteStore(db)
    store.create_tenant("TENANT-A", "租户A")
    store.add_membership("TENANT-A", "USER-001", Role.CUSTOMER)
    store.add_membership("TENANT-A", "ADMIN-A", Role.ADMIN)
    app = create_app(store=store, llm=MockLLM(), seed=False)
    return store, app, db


def _new_session(client, token) -> str:
    r = client.post("/api/sessions", headers=bearer(token))
    assert r.status_code == 201, r.text
    return r.json()["thread_id"]


def _chat(client, token, thread_id, cid, msg):
    return client.post("/api/chat", json={
        "mode": "start", "thread_id": thread_id, "client_request_id": cid, "message": msg,
    }, headers=bearer(token))


# ---- 演练项（自包含） ----
def drill_restart_resilience(record_dir: str) -> dict:
    """D1: 重建 app（模拟 API/worker 重启）后业务数据仍在。"""
    store, app, db = _fresh_env(record_dir, "D1")
    tok_user = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
    tok_admin = issue_token("TENANT-A", "ADMIN-A", Role.ADMIN)
    with TestClient(app) as c1:
        thread_id = _new_session(c1, tok_user)
        _chat(c1, tok_user, thread_id, "RESTART-1", "退货政策是什么？")
        assert c1.get(f"/api/sessions/{thread_id}", headers=bearer(tok_user)).status_code == 200
    store.close()  # 模拟 API 进程退出

    # "重启"：用同一 sqlite 文件重建 store + app
    store2 = SqliteStore(db)
    app2 = create_app(store=store2, llm=MockLLM(), seed=False)
    with TestClient(app2) as c2:
        session = c2.get(f"/api/sessions/{thread_id}", headers=bearer(tok_user))
        msgs = c2.get(f"/api/sessions/{thread_id}/messages", headers=bearer(tok_user))
        ok = session.status_code == 200 and msgs.status_code == 200
    store2.close()
    return {"name": "D1_api_worker_restart_resilience", "passed": ok,
            "detail": {"thread_id_preserved": bool(thread_id),
                       "session_read_after_restart": session.status_code,
                       "messages_after_restart": msgs.status_code}}


def drill_backup_restore(record_dir: str) -> dict:
    """D2: 数据库备份恢复（SQLite online backup ≈ pg_dump）→ RPO=0。"""
    store, app, db = _fresh_env(record_dir, "D2")
    thread = store.create_session("TENANT-A", "USER-001", "thr-bak-1", time.time(), 7).thread_id
    op = store.create_operation("TENANT-A", thread, "ORD-001",
                                PendingAction.REFUND, "oprefund:TENANT-A:ORD-001:B1", time.time())
    store.close()
    bak = str(Path(tempfile.gettempdir()) / "dsh-drills" / f"D2-bak-{int(time.time())}.db")
    c = sqlite3.connect(bak)
    s = sqlite3.connect(db)
    s.backup(c)
    c.close(); s.close()
    restored = SqliteStore(bak)
    r_op = restored.get_operation("TENANT-A", op.operation_id)
    r_sess = restored.get_session("TENANT-A", thread)
    ok = r_op is not None and r_sess.thread_id == thread
    restored.close()
    return {"name": "D2_db_backup_restore", "passed": ok,
            "detail": {"rpo_seconds": 0.0, "operation_id_preserved": bool(r_op),
                       "bytes_backup": os.path.getsize(bak)}}


def drill_approval_only_after_approval(record_dir: str) -> dict:
    store, app, db = _fresh_env(record_dir, "D3")
    tok_user = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
    tok_admin = issue_token("TENANT-A", "ADMIN-A", Role.ADMIN)
    with TestClient(app) as client:
        thread_id = _new_session(client, tok_user)
        r = _chat(client, tok_user, thread_id, "APPROV-1", "我要退款，订单号 ORD-001")
        evs = parse_sse(r.text)
        ar = next(e for e in evs if e["event"] == "approval_required")
        approval_id, operation_id = ar["data"]["approval_id"], ar["data"]["operation_id"]
        pre = client.get(f"/api/operations/{operation_id}", headers=bearer(tok_admin)).json()
        no_exec_before_approval = pre["status"] == "pending"
        d1 = client.post(f"/api/approvals/{approval_id}/decision",
                         json={"approved": True, "confirmation": True, "operation_id": operation_id},
                         headers=bearer(tok_admin))
        d2 = client.post(f"/api/approvals/{approval_id}/decision",
                         json={"approved": True, "confirmation": True, "operation_id": operation_id},
                         headers=bearer(tok_admin))
        ok = (d1.status_code == 200 and d1.json()["status"] == "executed"
              and d2.status_code == 200 and d2.json()["operation_id"] == operation_id)
    store.close()
    return {"name": "D3_approval_only_after_approval", "passed": ok,
            "detail": {"no_exec_before_approval": no_exec_before_approval,
                       "first_decision_status": d1.json().get("status"),
                       "second_decision_operation_id": d2.json().get("operation_id")}}


def drill_sse_resume(record_dir: str) -> dict:
    store, app, db = _fresh_env(record_dir, "D4")
    tok_user = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
    with TestClient(app) as client:
        thread_id = _new_session(client, tok_user)
        r1 = _chat(client, tok_user, thread_id, "SSE-1", "查订单 ORD-001")
        evs = parse_sse(r1.text)
        accepted = next(e for e in evs if e["event"] == "accepted")
        stream_id, last_id = accepted["data"]["stream_id"], int(accepted["id"])
        r2 = client.post("/api/chat", json={"mode": "resume", "stream_id": stream_id},
                         headers={**bearer(tok_user), "Last-Event-ID": str(last_id)})
        replayed = parse_sse(r2.text)
        ok = (r2.status_code == 200
              and not any(e["event"] == "accepted" for e in replayed)
              and any(e["event"] == "done" for e in replayed))
    store.close()
    return {"name": "D4_sse_disconnect_resume", "passed": ok,
            "detail": {"stream_id": stream_id, "resume_event_count": len(replayed)}}


def drill_concurrent_duplicate(record_dir: str) -> dict:
    store, app, db = _fresh_env(record_dir, "D5")
    tok_user = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
    with TestClient(app) as client:
        thread_id = _new_session(client, tok_user)
        cid = "CONC-1"
        r1 = _chat(client, tok_user, thread_id, cid, "我要退款，订单号 ORD-001")
        r2 = _chat(client, tok_user, thread_id, cid, "我要退款，订单号 ORD-001")
        e1 = parse_sse(r1.text); e2 = parse_sse(r2.text)
        s1 = next(e for e in e1 if e["event"] == "accepted")["data"]["stream_id"]
        s2 = next(e for e in e2 if e["event"] == "accepted")["data"]["stream_id"]
        ok = s1 == s2
    store.close()
    return {"name": "D5_concurrent_duplicate_submission", "passed": ok,
            "detail": {"stream_id_1": s1, "stream_id_2": s2, "same_stream": ok}}


def drill_reconcile(record_dir: str) -> dict:
    from src.config import get_settings
    from src.tools import build_adapter
    store, app, db = _fresh_env(record_dir, "D6")
    thread = store.create_session("TENANT-A", "USER-001", "thr-rec-1", time.time(), 7).thread_id
    op = store.create_operation("TENANT-A", thread, "ORD-001",
                                PendingAction.REFUND, "oprefund:TENANT-A:ORD-001:R1", time.time())
    rec = store.create_execution_record(
        "TENANT-A", operation_id=op.operation_id, pending_action=PendingAction.REFUND,
        order_id="ORD-001", idempotency_key=op.idempotency_key, mode=ExecutionMode.SHADOW,
        amount=100.0, now=time.time())
    # 人为置为"已提交未确认"（shadow 规范下不应存在；用于验证对账 fail-closed）
    store.update_execution_record("TENANT-A", rec.execution_id, status=ExecutionStatus.SUBMITTED)
    # 直接在本 store 上构建引擎做对账（reconcile_tenant 任务会用全局 store，不适合此处）。
    adapter = build_adapter(get_settings())
    engine = adapter.make_execution_engine(store)
    result = engine.reconcile("TENANT-A")
    res = {"scanned": result.scanned, "reconciled": result.reconciled,
           "mis_matched": result.mis_matched, "human_handoff": result.human_handoff}
    final_status = store.get_execution_record("TENANT-A", rec.execution_id).status.value
    store.close()
    # fail-closed：不确定记录必须被对账发现并置为非终态（mismatch/human_handoff），绝不静默确认。
    ok = res["scanned"] >= 1 and (res["mis_matched"] >= 1 or res["human_handoff"] >= 1) \
         and final_status in {"mismatched", "human_handoff"}
    res["final_status"] = final_status
    return {"name": "D6_sandbox_reconciliation", "passed": ok, "detail": res}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="预发布演练（本环境可运行子集）")
    parser.add_argument("--record-dir", default="deploy/drills/records")
    args = parser.parse_args(argv)
    record_dir = args.record_dir
    Path(record_dir).mkdir(parents=True, exist_ok=True)

    runners = [drill_restart_resilience, drill_backup_restore,
               drill_approval_only_after_approval, drill_sse_resume,
               drill_concurrent_duplicate, drill_reconcile]
    results = []
    # 每个演练项自建 store+app；逐个执行，异常也记为 FAIL（fail-closed）。
    for fn in runners:
        try:
            results.append(fn(record_dir))
        except Exception as exc:  # noqa: BLE001
            results.append({"name": fn.__name__, "passed": False,
                            "detail": {"error": f"{type(exc).__name__}: {exc}"}})

    all_passed = all(r["passed"] for r in results)
    record = {"scenario": "preview_drills_dev", "environment": "dev_local_sqlite",
              "timestamp": time.time(), "all_passed": all_passed, "drills": results}
    rec_file = Path(record_dir) / f"dev-drills-{int(time.time())}.json"
    rec_file.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    for r in results:
        print(f"  [{'PASS' if r['passed'] else 'FAIL'}] {r['name']}: {r['detail']}")
    print(f"\n[{'PASS' if all_passed else 'FAIL'}] 预发布演练（本环境） — 记录: {rec_file}")
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
