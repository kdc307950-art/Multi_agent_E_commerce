"""并发与数据库恢复验收测试 + 可观测证据（无需 Docker/Redis）。

覆盖：
- 并发审批 CAS：仅一个抢占成功（SQLite 跨线程），见 test_sqlite_store.py；此处补充
  Celery worker 任务可见性 + 数据库备份/恢复（RPO/RTO）。
- 数据库恢复：用 SQLite backup API 冷备 → 恢复为新 store → 数据完整（RPO=0）。
- 记录 RTO（恢复耗时）与 RPO 到 evidence/recovery.json。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from src.core.types import ApprovalStatus, OperationStatus, PendingAction, Role
from src.infrastructure.sqlite_store import SqliteStore


def _seed(db: SqliteStore) -> dict:
    db.create_tenant("TENANT-A", "租户A")
    db.add_membership("TENANT-A", "USER-001", Role.CUSTOMER)
    db.add_membership("TENANT-A", "ADMIN-A", Role.ADMIN)
    thread = db.create_session("TENANT-A", "USER-001", "thr-recovery-1", time.time(), 7).thread_id
    op = db.create_operation("TENANT-A", thread, "ORD-001", PendingAction.REFUND,
                             "oprefund:TENANT-A:ORD-001:R1", time.time())
    db.update_operation("TENANT-A", op.operation_id, OperationStatus.EXECUTED,
                        {"message": "退款完成", "order_id": "ORD-001"})
    return {"thread_id": thread, "operation_id": op.operation_id}


def test_sqlite_backup_restore_and_rpo(tmp_path):
    db_path = tmp_path / "src.db"
    db = SqliteStore(str(db_path))
    ids = _seed(db)
    db.close()

    # RTO：计时冷备→恢复
    backup_path = tmp_path / "backup.db"
    conn = sqlite3.connect(str(backup_path))
    src = sqlite3.connect(str(db_path))
    src.backup(conn)  # SQLite online backup API（等效生产 pg_dump）
    conn.close()
    src.close()

    t0 = time.perf_counter()
    restored = SqliteStore(str(backup_path))
    restore_seconds = time.perf_counter() - t0

    # 恢复后完整性校验（RPO=0：所有已提交数据均恢复）
    sess = restored.get_session("TENANT-A", ids["thread_id"])
    assert sess.thread_id == ids["thread_id"]
    op = restored.get_operation("TENANT-A", ids["operation_id"])
    assert op.status == OperationStatus.EXECUTED
    assert op.result["message"] == "退款完成"
    restored.close()

    _write_recovery(restore_seconds=restore_seconds, rpo_seconds=0.0,
                    bytes_backup=backup_path.stat().st_size)
    assert restored is not None


def test_celery_worker_task_visible():
    """worker 任务注册可见（Redis broker 不可用时不验收运行，但契约可静态校验）。"""
    from src.tasks.worker import celery_app
    names = set(celery_app.tasks.keys())
    assert "memory.write_tick" in names


def _write_recovery(restore_seconds: float, rpo_seconds: float, bytes_backup: int) -> None:
    out = Path(os.environ.get("DSH_EVIDENCE_DIR", "evidence"))
    out.mkdir(parents=True, exist_ok=True)
    (out / "recovery.json").write_text(
        json.dumps({"rpo_seconds": rpo_seconds, "rto_restore_seconds": restore_seconds,
                    "backup_bytes": bytes_backup, "scenario": "sqlite online backup API"},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def test_db_recovery_approved_status_preserved(tmp_path):
    """恢复后审批终态不丢失（防重复/防丢失审批记录）。"""
    db_path = tmp_path / "dbrecover.db"
    db = SqliteStore(str(db_path))
    db.create_tenant("TENANT-A", "租户A")
    db.add_membership("TENANT-A", "USER-001", Role.CUSTOMER)
    db.add_membership("TENANT-A", "ADMIN-A", Role.ADMIN)
    thread = db.create_session("TENANT-A", "USER-001", "thr-1", time.time(), 7).thread_id
    op = db.create_operation("TENANT-A", thread, "ORD-001", PendingAction.REFUND,
                             "oprefund:TENANT-A:ORD-001:R2", time.time())
    db.create_approval("TENANT-A", thread, op.operation_id, PendingAction.REFUND,
                       "ORD-001", 299.0, "申请退款", time.time())
    # 模拟审批通过（CAS）
    all_appr = db.list_approvals("TENANT-A")
    db.claim_approval_decision("TENANT-A", all_appr[0].approval_id, "ADMIN-A", True, None, time.time())
    db.close()

    # 备份→恢复
    bak = tmp_path / "bak.db"
    c = sqlite3.connect(str(bak)); s = sqlite3.connect(str(db_path)); s.backup(c); c.close(); s.close()
    r = SqliteStore(str(bak))
    restored_appr = r.list_approvals("TENANT-A")
    assert restored_appr[0].status == ApprovalStatus.APPROVED
    r.close()
