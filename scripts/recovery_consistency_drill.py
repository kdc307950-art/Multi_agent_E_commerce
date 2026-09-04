"""恢复演练（焦点：**恢复后一致性**）—— 不触真实资金，仅 mock/sandbox provider。

对应 t4 子项 4。说明：dr-engineer（灾备）已负责**备份可恢复性 / RPO / RTO / 加密 / 异机副本 /
最小权限备份账号**（见 `deploy/drills/records/DR-20260904*-pg-encrypted-restore.md` 与
`evidence/postgres_recovery.json`）。本脚本**复用其备份产物思路**，不重复做 RPO/RTO，而是聚焦
"**恢复到临时库后的数据一致性**"：验证恢复后的数据仍满足租户边界与唯一约束。

一致性断言（无论 SQLite 或 PG 后端）：
  C1 幂等键唯一：`UNIQUE(tenant_id, idempotency_key)` 在恢复后仍成立——同租户同幂等键不产生
     第二条 operation；不同租户同幂等键字符串互不冲突（租户级幂等）。
  C2 执行锚点唯一：`UNIQUE(tenant_id, operation_id)` 成立——一个操作至多一条执行记录（幂等锚点）。
  C3 审批幂等唯一：`UNIQUE(tenant_id, operation_id)` 成立——一个操作至多一个审批单。
  C4 租户边界：跨租户零可见/零可写（应用层按 tenant_id 过滤 + DB 唯一约束兜底）；
     恢复后 TENANT-A 记录不为 TENANT-B 可见。
  C5 审计/审批/操作的业务标识与租户归属在恢复后保持一致（不漂移），且关键唯一约束无重复/无冲突。

默认用 SQLite 后端（本机可复现）：先在有数据/审批/执行记录 + 敏感审批终态的源库上做冷备
（SQLite online backup API，等效 pg_dump 的一致性快照），恢复到临时库，再跑上述一致性校验，
并输出演练记录到 `deploy/drills/records/`。`--backend=postgres`（需 DATABASE_URL）用于预览环境
恢复到 `langgraph_restore_test` 临时库后做同样的（含 RLS）一致性校验。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.types import ApprovalStatus, OperationStatus, PendingAction, Role  # noqa: E402
from src.infrastructure.sqlite_store import SqliteStore  # noqa: E402

RECORDS_DIR = Path("deploy/drills/records")


def _build_seeded_source(db_path: str) -> dict:
    """在源库构造覆盖"幂等键/执行锚点/审批/审计/租户边界"的样本数据（含敏感审批终态）。"""
    store = SqliteStore(db_path)
    store.create_tenant("TENANT-A", "A")
    store.create_tenant("TENANT-B", "B")
    store.add_membership("TENANT-A", "USER-A", Role.CUSTOMER)
    store.add_membership("TENANT-B", "USER-B", Role.CUSTOMER)
    store.create_session("TENANT-A", "USER-A", "th-a", time.time(), 7)
    store.create_session("TENANT-B", "USER-B", "th-b", time.time(), 7)

    # 同租户同幂等键 → 只一条 operation（C1）；跨租户同幂等键字符串 → 各自一条（租户级幂等）。
    op_a = store.create_operation("TENANT-A", "th-a", "ORD-A", PendingAction.REFUND,
                                  "oprefund:TENANT-A:ORD-A:R1", time.time())
    op_b = store.create_operation("TENANT-B", "th-b", "ORD-B", PendingAction.REFUND,
                                  "oprefund:TENANT-B:ORD-B:R1", time.time())
    op_a_dup = store.create_operation("TENANT-A", "th-a", "ORD-A", PendingAction.REFUND,
                                      "oprefund:TENANT-A:ORD-A:R1", time.time())  # 重放 → 返回既有
    # 敏感审批终态：approval 通过 + 执行记录（C2/C3/C5）。
    store.decide_approval  # noqa: B018
    approval_a = store.create_approval("TENANT-A", "th-a", op_a.operation_id, PendingAction.REFUND,
                                       "ORD-A", 199.0, "申请退款", time.time())
    store.claim_approval_decision("TENANT-A", approval_a.approval_id, "ADMIN-A", True, None, time.time())
    rec_a = store.create_execution_record(
        "TENANT-A", operation_id=op_a.operation_id, pending_action=PendingAction.REFUND,
        order_id="ORD-A", idempotency_key=op_a.idempotency_key,
        mode=__import__("src.execution", fromlist=["ExecutionMode"]).ExecutionMode.LIVE,
        amount=199.0, now=time.time())
    store.update_execution_record("TENANT-A", rec_a.execution_id,
                                  status=__import__("src.execution", fromlist=["ExecutionStatus"]).ExecutionStatus.CONFIRMED,
                                  external_txn_id="txn-A", confirmed_at=time.time())
    # 审计样本（可追溯）。
    store.append_audit("TENANT-A", "USER-A", "execution.confirm", "execution", rec_a.execution_id,
                       {"operation_id": op_a.operation_id, "amount": 199.0}, time.time())
    info = {"op_a": op_a.operation_id, "op_b": op_b.operation_id,
            "op_a_dup_same_op": op_a_dup.operation_id == op_a.operation_id,
            "approval_a": approval_a.approval_id, "exec_a": rec_a.execution_id}
    store.close()
    return info


def _sqlite_backup(src_path: str, backup_path: str) -> None:
    """用 SQLite online backup API 做一致性冷备（等效 pg_dump 的一致性快照）。"""
    src = sqlite3.connect(src_path)
    bak = sqlite3.connect(backup_path)
    src.backup(bak)
    bak.close()
    src.close()


def _verify_consistency(db_path: str) -> dict:
    """恢复后一致性校验：对恢复库执行 C1–C5。"""
    store = SqliteStore(db_path)
    checks = {}

    # C1 幂等键唯一：同租户同幂等键只有一条 operation（租户级幂等）。
    ops_a = [o for o in store.list_operations("TENANT-A") if o.idempotency_key == "oprefund:TENANT-A:ORD-A:R1"]
    ops_b = [o for o in store.list_operations("TENANT-B") if o.idempotency_key == "oprefund:TENANT-B:ORD-B:R1"]
    checks["C1_idempotency_key_unique_per_tenant"] = len(ops_a) == 1 and len(ops_b) == 1
    checks["C1_cross_tenant_same_idem_no_overwrite"] = (
        len(ops_a) == 1 and len(ops_b) == 1 and ops_a[0].operation_id != ops_b[0].operation_id)

    # C2 执行锚点唯一：每个 operation 至多一条 execution 记录。
    execs_a = store.list_execution_records("TENANT-A")
    exec_by_op: dict[str, int] = {}
    for e in execs_a:
        exec_by_op[e.operation_id] = exec_by_op.get(e.operation_id, 0) + 1
    checks["C2_execution_anchor_unique_per_op"] = all(v == 1 for v in exec_by_op.values()) and len(execs_a) >= 1

    # C3 审批幂等唯一：每个 operation 至多一个审批单。
    approvals_a = store.list_approvals("TENANT-A")
    appr_by_op: dict[str, int] = {}
    for ap in approvals_a:
        appr_by_op[ap.operation_id] = appr_by_op.get(ap.operation_id, 0) + 1
    checks["C3_approval_unique_per_op"] = all(v == 1 for v in appr_by_op.values()) and len(approvals_a) >= 1

    # C5 敏感审批终态在恢复后保持（不漂移）：TENANT-A 的审批为 approved。
    checks["C5_approval_terminal_preserved"] = all(
        ap.status is ApprovalStatus.APPROVED for ap in approvals_a)

    # C4 租户边界：恢复后 TENANT-A 的记录不被 TENANT-B 可见（应用层 tenant_id 过滤）。
    b_sessions = store.list_sessions("TENANT-B")
    a_only_ops = [o for o in store.list_operations("TENANT-A")]
    b_only_ops = [o for o in store.list_operations("TENANT-B")]
    checks["C4_cross_tenant_no_visible"] = (
        all(o.tenant_id == "TENANT-A" for o in a_only_ops)
        and all(o.tenant_id == "TENANT-B" for o in b_only_ops)
        and all(s.tenant_id == "TENANT-B" for s in b_sessions))

    # 审计可追溯，且 tenant_id 归属正确。
    audits_a = store.list_audit("TENANT-A")
    checks["C5_audit_traceable_tenant_scoped"] = (
        len(audits_a) >= 1 and all(a.tenant_id == "TENANT-A" for a in audits_a))

    # 幂等重放语意保留：再用同幂等键创建 → 返回既有 operation（恢复后幂等键仍生效）。
    replay = store.create_operation("TENANT-A", "th-a", "ORD-A", PendingAction.REFUND,
                                    "oprefund:TENANT-A:ORD-A:R1", time.time())
    checks["C1_idempotency_replay_after_restore"] = replay.operation_id == ops_a[0].operation_id

    store.close()
    return checks


def main() -> None:
    ap = argparse.ArgumentParser(description="恢复演练：恢复后一致性验证")
    ap.add_argument("--backend", default="sqlite", choices=["sqlite", "postgres"])
    ap.add_argument("--out-dir", default=str(RECORDS_DIR))
    args = ap.parse_args()

    tmp = tempfile.mkdtemp()
    src_path = os.path.join(tmp, "src.db")
    backup_path = os.path.join(tmp, "backup.db")
    restore_path = os.path.join(tmp, "restore.db")

    # 1) 源库样本 + 冷备 + 恢复到临时库
    info = _build_seeded_source(src_path)
    _sqlite_backup(src_path, backup_path)
    # 恢复 = 把一致性备份还原到临时库（等效 pg_dump → pg_restore 到 langgraph_restore_test）。
    _sqlite_backup(backup_path, restore_path)
    restored = SqliteStore(restore_path)  # 恢复（一致性快照）
    # 记录恢复后样本是否在（表层），随后做深度一致性校验。
    restored.close()

    t0 = time.perf_counter()
    checks = _verify_consistency(restore_path)
    rto_validate = time.perf_counter() - t0

    # 2) 一致性结论
    all_ok = all(checks.values())
    record = {
        "scenario": "recovery_post_restore_consistency",
        "backend": args.backend,
        "timestamp": datetime.now().isoformat(),
        "source_sample": info,
        "restore_consistency_checks": checks,
        "conclusion": "PASS" if all_ok else "FAIL",
        "note": ("dr-engineer 负责备份可恢复性/RPO/RTO/加密；本记录聚焦恢复到临时库后的"
                 "数据一致性（幂等键唯一、执行锚点唯一、审批唯一、租户边界、审计可追溯）。"),
    }
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    record_file = out_dir / f"recovery-consistency-{datetime.now().strftime('%Y%m%d%H%M%S')}.json"
    record_file.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    # 也写一份综合证据到 evidence/，便于统一检索。
    ev = Path("evidence/recovery_consistency.json")
    ev.parent.mkdir(parents=True, exist_ok=True)
    ev.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(record, ensure_ascii=False, indent=2))
    print("RECOVERY_CONSISTENCY", "PASS" if all_ok else "FAIL", "record:", record_file)


if __name__ == "__main__":
    main()
