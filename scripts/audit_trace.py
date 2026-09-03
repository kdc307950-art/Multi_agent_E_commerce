"""audit_trace.py — 平台级审计追溯（platform_admin 受控）、跨租户四维定位。

按 租户 / 会话 / 审批 / operation_id 追溯关键审计，用于合规复核与事故定位。
注意：这是**平台级**工具，仅 platform_admin 受控流程使用，不走普通租户接口；detail 输出已脱敏。

用法（对本地 sqlite 数据面演示；对预发布 PostgreSQL 可设 DATABASE_URL 走 PostgresStore）：
    python scripts/audit_trace.py --db <sqlite-path> --tenant TENANT-A \
        [--thread thr-...] [--approval appr-...] [--operation op-...] [--action approve] [--n 50]
若未指定 --db，则基于内存 store 注入若干示例审计并将结果写 evidence/audit_trace.json。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 导入所需的脱敏工具与存储
from src.core.types import Role  # noqa: E402
from src.infrastructure.store import MemoryStore, filter_audit  # noqa: E402
from src.infrastructure.sqlite_store import SqliteStore  # noqa: E402
from src.llm.security import redact  # noqa: E402


def _redact_detail(detail: dict) -> dict:
    """深脱敏 detail（地址/支付/卡号等所有字符串）。"""
    if not isinstance(detail, dict):
        return detail
    return {k: (redact(v) if isinstance(v, str) else
                ({kk: redact(vv) for kk, vv in v.items()} if isinstance(v, dict) else v))
            for k, v in detail.items()}


def _store_from_args(args):
    if args.db and os.path.exists(args.db):
        return SqliteStore(args.db)
    return MemoryStore()


def _seed_demo(store):
    """向内存 store 注入示例审计，演示四维追溯。"""
    store.create_tenant("TENANT-A", "租户A")
    store.add_membership("TENANT-A", "USER-001", Role.CUSTOMER)
    store.add_membership("TENANT-A", "ADMIN-A", Role.ADMIN)
    t = time.time()
    thread = "thr-demo-1"
    op = "op-a1b2c3"
    appr = "appr-0001"
    # 按 session / approval / operation 三类写审计
    store.append_audit("TENANT-A", "USER-001", "session.create", "session", thread,
                       {"thread_id": thread}, t)
    store.append_audit("TENANT-A", "USER-001", "approval.request", "approval", appr,
                       {"thread_id": thread, "operation_id": op,
                        "address": "北京市朝阳区建国路88号", "amount": 299.0}, t)
    store.append_audit("TENANT-A", "ADMIN-A", "approval.decide", "approval", appr,
                       {"thread_id": thread, "operation_id": op, "approved": True}, t + 1)
    store.append_audit("TENANT-A", "ADMIN-A", "execution.submit", "operation", op,
                       {"thread_id": thread, "operation_id": op,
                        "card": "6222000011112222"}, t + 2)
    return {"thread": thread, "operation": op, "approval": appr}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="平台级审计追溯")
    parser.add_argument("--db", default="", help="sqlite 数据面路径（缺省用内存示例注入）")
    parser.add_argument("--tenant", default="TENANT-A")
    parser.add_argument("--thread", default=None)
    parser.add_argument("--approval", default=None)
    parser.add_argument("--operation", default=None)
    parser.add_argument("--action", default=None)
    parser.add_argument("--n", type=int, default=50)
    parser.add_argument("--out", default="evidence/audit_trace.json")
    args = parser.parse_args(argv)

    store = _store_from_args(args)
    demo = None
    if not args.db:
        demo = _seed_demo(store)

    target_type = None
    target_id = None
    if args.approval:
        target_type, target_id = "approval", args.approval
    elif args.operation:
        target_type, target_id = "operation", args.operation

    records = store.search_audit(args.tenant, thread_id=args.thread,
                                 target_type=target_type, target_id=target_id,
                                 action=args.action, limit=args.n)

    out = {
        "tool": "audit_trace",
        "tenant_id": args.tenant,
        "filters": {"thread_id": args.thread, "approval_id": args.approval,
                    "operation_id": args.operation, "action": args.action},
        "record_count": len(records),
        "records": [
            {"audit_id": r.audit_id, "tenant_id": r.tenant_id, "user_id": r.user_id,
             "action": r.action, "target_type": r.target_type, "target_id": r.target_id,
             "detail": _redact_detail(r.detail), "created_at": r.created_at}
            for r in records
        ],
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    for r in out["records"]:
        print(f"  [{r['target_type']}:{r['target_id']}] {r['action']} by {r['user_id']} "
              f"-> {json.dumps(r['detail'], ensure_ascii=False)}")
    print(f"\n审计追溯（{args.tenant}）记录 {out['record_count']} 条 -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
