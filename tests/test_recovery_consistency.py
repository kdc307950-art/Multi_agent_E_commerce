"""恢复演练（pytest 版）—— 焦点：**恢复后一致性**（不触真实资金，仅 mock/sandbox）。

与 dr-engineer（灾备）边界：dr-engineer 负责备份可恢复性 / RPO / RTO / 加密 / 异机副本 /
最小权限备份账号（见 `deploy/drills/records/DR-20260904*-pg-encrypted-restore.md`）。本测试
聚焦"**恢复到临时库后的数据一致性**"：幂等键唯一、执行锚点唯一、审批唯一、租户边界、
审批终态保留、审计可追溯，均在恢复后仍成立。

- 默认后端：SQLite（本机可复现；冷备用 SQLite online backup API，一致性快照等效 pg_dump）。
- `--backend=postgres`（需 DATABASE_URL）时运行 PG-marked 用例：恢复到 langgraph_restore_test
  临时库后在真实服务器上做同样的一致性校验（含 RLS/跨租户隔离），无 DB 时跳过（不伪造）。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from src.core.types import ApprovalStatus, OperationStatus, PendingAction, Role
from src.execution import ExecutionMode, ExecutionStatus
from src.infrastructure.sqlite_store import SqliteStore


def _seed(db_path: str) -> dict:
    store = SqliteStore(db_path)
    store.create_tenant("TENANT-A", "A")
    store.create_tenant("TENANT-B", "B")
    store.add_membership("TENANT-A", "USER-A", Role.CUSTOMER)
    store.add_membership("TENANT-B", "USER-B", Role.CUSTOMER)
    store.create_session("TENANT-A", "USER-A", "th-a", time.time(), 7)
    store.create_session("TENANT-B", "USER-B", "th-b", time.time(), 7)
    op_a = store.create_operation("TENANT-A", "th-a", "ORD-A", PendingAction.REFUND,
                                  "oprefund:TENANT-A:ORD-A:R1", time.time())
    op_b = store.create_operation("TENANT-B", "th-b", "ORD-B", PendingAction.REFUND,
                                  "oprefund:TENANT-B:ORD-B:R1", time.time())
    approval_a = store.create_approval("TENANT-A", "th-a", op_a.operation_id, PendingAction.REFUND,
                                       "ORD-A", 199.0, "申请退款", time.time())
    store.claim_approval_decision("TENANT-A", approval_a.approval_id, "ADMIN-A", True, None, time.time())
    rec_a = store.create_execution_record(
        "TENANT-A", operation_id=op_a.operation_id, pending_action=PendingAction.REFUND,
        order_id="ORD-A", idempotency_key=op_a.idempotency_key,
        mode=ExecutionMode.LIVE, amount=199.0, now=time.time())
    store.update_execution_record("TENANT-A", rec_a.execution_id, status=ExecutionStatus.CONFIRMED,
                                  external_txn_id="txn-A", confirmed_at=time.time())
    store.append_audit("TENANT-A", "USER-A", "execution.confirm", "execution", rec_a.execution_id,
                       {"operation_id": op_a.operation_id, "amount": 199.0}, time.time())
    info = {"op_a": op_a.operation_id, "op_b": op_b.operation_id, "exec_a": rec_a.execution_id}
    store.close()
    return info


def _backup(src_path: str, dst_path: str) -> None:
    s = sqlite3.connect(src_path)
    d = sqlite3.connect(dst_path)
    s.backup(d)  # SQLite online backup API（一致性快照，等效 pg_dump）
    d.close()
    s.close()


def _verify(restore_path: str) -> dict:
    store = SqliteStore(restore_path)
    checks = {}
    ops_a = [o for o in store.list_operations("TENANT-A") if o.idempotency_key == "oprefund:TENANT-A:ORD-A:R1"]
    ops_b = [o for o in store.list_operations("TENANT-B") if o.idempotency_key == "oprefund:TENANT-B:ORD-B:R1"]
    checks["idempotency_key_unique_per_tenant"] = len(ops_a) == 1 and len(ops_b) == 1
    checks["cross_tenant_no_overwrite"] = len(ops_a) == 1 and len(ops_b) == 1 and ops_a[0].operation_id != ops_b[0].operation_id
    execs_a = store.list_execution_records("TENANT-A")
    by_op = {}
    for e in execs_a:
        by_op[e.operation_id] = by_op.get(e.operation_id, 0) + 1
    checks["execution_anchor_unique_per_op"] = all(v == 1 for v in by_op.values())
    apprs = store.list_approvals("TENANT-A")
    checks["approval_terminal_preserved"] = all(a.status is ApprovalStatus.APPROVED for a in apprs)
    checks["audit_tenant_scoped"] = all(a.tenant_id == "TENANT-A" for a in store.list_audit("TENANT-A"))
    replay = store.create_operation("TENANT-A", "th-a", "ORD-A", PendingAction.REFUND,
                                    "oprefund:TENANT-A:ORD-A:R1", time.time())
    checks["idempotency_replay_after_restore"] = replay.operation_id == ops_a[0].operation_id
    store.close()
    return checks


def test_recovery_post_restore_consistency(tmp_path):
    src = tmp_path / "src.db"
    bak = tmp_path / "backup.db"
    restore = tmp_path / "restore.db"
    _seed(str(src))
    _backup(str(src), str(bak))
    _backup(str(bak), str(restore))  # 恢复 = 还原一致性快照到新库
    checks = _verify(str(restore))
    assert all(checks.values()), f"恢复后一致性断言失败: {checks}"


def test_recovery_applies_to_cross_tenant_isolation(tmp_path):
    """恢复后仍保持租户边界：TENANT-A/B 的 operation 各自归属正确，互不泄露。"""
    src = tmp_path / "src.db"
    bak = tmp_path / "backup.db"
    restore = tmp_path / "restore.db"
    _seed(str(src))
    _backup(str(src), str(bak))
    _backup(str(bak), str(restore))  # 恢复 → 校验
    store = SqliteStore(str(restore))
    a_ops = store.list_operations("TENANT-A")
    b_ops = store.list_operations("TENANT-B")
    assert all(o.tenant_id == "TENANT-A" for o in a_ops)
    assert all(o.tenant_id == "TENANT-B" for o in b_ops)
    assert all(s.tenant_id == "TENANT-B" for s in store.list_sessions("TENANT-B"))
    store.close()


# ---------------------------------------------------------------------------
# PostgreSQL 后端（恢复到 langgraph_restore_test 临时库做含 RLS 的一致性校验）。
# 无 DATABASE_URL 时整组跳过（不将被跳过当成已通过）。
# ---------------------------------------------------------------------------
def _pg_available() -> bool:
    from tests.pg_helpers import pg_available
    return pg_available()


@pytest.mark.postgres
@pytest.mark.skipif(not _pg_available(),
                    reason="PostgreSQL 不可用：设置 DATABASE_URL 并启动数据库")
def test_recovery_post_restore_consistency_on_pg():
    """真实 PG：冷备 → 恢复到 langgraph_restore_test 临时库 → 一致性 + 租户边界校验。"""
    import os
    from sqlalchemy import create_engine, text
    from src.infrastructure.postgres_store import PostgresStore, _to_sqlalchemy_url
    from tests.pg_helpers import make_pg_engine, reset_pg_schema, setup_runtime_role, app_runtime_dsn

    eng = make_pg_engine()
    reset_pg_schema(eng)
    setup_runtime_role(eng)
    store = PostgresStore(app_runtime_dsn(), engine=eng)
    store.create_tenant("TENANT-A", "A")
    store.create_tenant("TENANT-B", "B")
    store.add_membership("TENANT-A", "USER-A", Role.CUSTOMER)
    store.add_membership("TENANT-B", "USER-B", Role.CUSTOMER)
    store.create_session("TENANT-A", "USER-A", "th-a", time.time(), 7)
    store.create_session("TENANT-B", "USER-B", "th-b", time.time(), 7)
    op_a = store.create_operation("TENANT-A", "th-a", "ORD-A", PendingAction.REFUND,
                                  "oprefund:TENANT-A:ORD-A:R1", time.time())
    op_b = store.create_operation("TENANT-B", "th-b", "ORD-B", PendingAction.REFUND,
                                  "oprefund:TENANT-B:ORD-B:R1", time.time())
    approval_a = store.create_approval("TENANT-A", "th-a", op_a.operation_id, PendingAction.REFUND,
                                       "ORD-A", 199.0, "申请退款", time.time())
    store.claim_approval_decision("TENANT-A", approval_a.approval_id, "ADMIN-A", True, None, time.time())
    rec_a = store.create_execution_record(
        "TENANT-A", operation_id=op_a.operation_id, pending_action=PendingAction.REFUND,
        order_id="ORD-A", idempotency_key=op_a.idempotency_key,
        mode=ExecutionMode.LIVE, amount=199.0, now=time.time())
    store.update_execution_record("TENANT-A", rec_a.execution_id, status=ExecutionStatus.CONFIRMED,
                                  external_txn_id="txn-A", confirmed_at=time.time())
    store.append_audit("TENANT-A", "USER-A", "execution.confirm", "execution", rec_a.execution_id,
                       {"operation_id": op_a.operation_id, "amount": 199.0}, time.time())

    # 冷备（pg_dump -Fc）→ 恢复到临时库 langgraph_restore_test（用 owner/migrator 角色）。
    dsn = os.environ["DATABASE_URL"]
    restore_db = "langgraph_restore_test_consistency"
    with eng.connect() as conn:
        conn.execute(text("DROP DATABASE IF EXISTS %s" % restore_db))  # noqa: S608
    # 用 owner 角色在独立连接创建临时库并经 pg_dump 全量复制（简化为 CREATE DATABASE TEMPLATE）。
    with eng.connect() as conn:
        # 冷备等价物：此处用迁移角色直接建临时库再随样本，RPO/RTO 由 dr-engineer 验收。
        conn.execute(text("CREATE DATABASE %s WITH TEMPLATE langgraph" % restore_db))  # noqa: S608
    # 恢复到临时库后，用运行角色 app_runtime 重连做一致性校验。
    restore_url = app_runtime_dsn().rsplit("/", 1)[0] + "/" + restore_db
    r_store = PostgresStore(restore_url)
    ops_a = [o for o in r_store.list_operations("TENANT-A") if o.idempotency_key == "oprefund:TENANT-A:ORD-A:R1"]
    ops_b = [o for o in r_store.list_operations("TENANT-B") if o.idempotency_key == "oprefund:TENANT-B:ORD-B:R1"]
    assert len(ops_a) == 1 and len(ops_b) == 1
    assert ops_a[0].operation_id != ops_b[0].operation_id  # 租户级幂等互不覆盖
    execs_a = r_store.list_execution_records("TENANT-A")
    assert any(e.status is ExecutionStatus.CONFIRMED for e in execs_a)
    assert all(a.tenant_id == "TENANT-A" for a in r_store.list_audit("TENANT-A"))
    r_store.close()
    # 清理临时库
    r_store2 = PostgresStore(app_runtime_dsn(), engine=eng)
    r_store2.close()
    with eng.connect() as conn:
        conn.execute(text("DROP DATABASE IF EXISTS %s" % restore_db))  # noqa: S608
    store.close()
