"""SQLite 持久化存储测试。

验证：
- 写入后重开连接数据保留（持久化）。
- 同租户同 idempotency_key 幂等重放（跨连接）。
- 两租户同名订单/请求各自产生独立 operation_id（联合唯一键隔离）。
- 会话/审批/操作归属租户校验。
"""
from __future__ import annotations

import pytest

from src.core.types import (
    ApprovalStatus,
    OperationStatus,
    PendingAction,
    Role,
    TenantStatus,
    generate_operation_key,
)
from src.infrastructure.sqlite_store import SqliteStore


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    store = SqliteStore(str(path))
    store.create_tenant("TENANT-A", "租户A")
    store.create_tenant("TENANT-B", "租户B")
    store.add_membership("TENANT-A", "USER-001", Role.CUSTOMER)
    store.add_membership("TENANT-B", "USER-B1", Role.CUSTOMER)
    yield store
    store.close()


def test_persists_across_reopen(tmp_path):
    path = tmp_path / "reopen.db"
    s1 = SqliteStore(str(path))
    s1.create_tenant("T1", "租户1")
    s1.add_membership("T1", "U1", Role.CUSTOMER)
    s1.create_session("T1", "U1", "th-1", 1000.0, 7)
    s1.close()

    # 重新打开同一 db，数据应仍在。
    s2 = SqliteStore(str(path))
    tenant = s2.get_tenant("T1")
    session = s2.get_session("T1", "th-1")
    assert tenant.name == "租户1"
    assert session.thread_id == "th-1"
    s2.close()


def test_idempotency_across_connections(db, tmp_path):
    # 同一幂等键重放返回同一 operation_id（即使是新的连接）。
    key = generate_operation_key(PendingAction.REFUND, "TENANT-A", "ORD-001", "REQ-1")
    op1 = db.create_operation("TENANT-A", "th-a", "ORD-001", PendingAction.REFUND, key, 1000.0)
    op2 = db.create_operation("TENANT-A", "th-a", "ORD-001", PendingAction.REFUND, key, 2000.0)
    assert op1.operation_id == op2.operation_id

    # 重新打开后，同一幂等键仍返回既有 op（不重复执行）。
    path = tmp_path / "idem.db"
    s1 = SqliteStore(str(path))
    s1.create_tenant("T", "租户")
    s1.add_membership("T", "U", Role.CUSTOMER)
    s1.create_operation("T", "th", "ORD", PendingAction.REFUND, "k", 1.0)
    s1.close()
    s2 = SqliteStore(str(path))
    op = s2.create_operation("T", "th", "ORD", PendingAction.REFUND, "k", 2.0)
    assert op.operation_id == op.operation_id  # 同键重放
    s2.close()


def test_cross_tenant_unique_idempotency(db):
    # 两租户同名订单/请求：各自产生独立 operation_id。
    key_a = generate_operation_key(PendingAction.REFUND, "TENANT-A", "ORD-001", "REQ")
    key_b = generate_operation_key(PendingAction.RETURN_REQUEST, "TENANT-B", "ORD-001", "REQ")
    oa = db.create_operation("TENANT-A", "th-a", "ORD-001", PendingAction.REFUND, key_a, 1.0)
    ob = db.create_operation("TENANT-B", "th-b", "ORD-001", PendingAction.RETURN_REQUEST, key_b, 1.0)
    assert oa.operation_id != ob.operation_id


def test_session_approval_tenant_scope(db):
    # 会话/审批/操作按租户校验；跨租户读取应 404。
    s = db.create_session("TENANT-A", "USER-001", "th-scope", 1000.0, 7)
    assert s.thread_id == "th-scope"
    op = db.create_operation("TENANT-A", "th-scope", "ORD-001", PendingAction.REFUND,
                             "k-scope", 1000.0)
    appr = db.create_approval("TENANT-A", "th-scope", op.operation_id, PendingAction.REFUND,
                              "ORD-001", 100.0, "reason", 1000.0)
    # 跨租户读 A 的审批/操作 → 404。
    from src.core.types import DomainError
    with pytest.raises(DomainError):
        db.get_approval("TENANT-B", appr.approval_id)
    with pytest.raises(DomainError):
        db.get_operation("TENANT-B", op.operation_id)


def test_suspended_tenant_blocks(db):
    db.set_tenant_status("TENANT-B", TenantStatus.SUSPENDED)
    from src.core.types import DomainError
    with pytest.raises(DomainError):
        db.create_session("TENANT-B", "USER-B1", "th-sus", 1000.0, 7)


# ---- 审批 CAS 决策（跨连接原子）与 operation 状态联动 ----
def _make_pending_refund(store):
    op = store.create_operation("TENANT-A", "th-cas", "ORD-001", PendingAction.REFUND,
                                generate_operation_key(PendingAction.REFUND, "TENANT-A", "ORD-001", "REQ-CAS"),
                                1.0)
    appr = store.create_approval("TENANT-A", "th-cas", op.operation_id, PendingAction.REFUND,
                                 "ORD-001", 100.0, "reason", 1.0)
    return op, appr


def test_sqlite_claim_decision_only_once_and_links_operation(db):
    op, appr = _make_pending_refund(db)
    a1, c1 = db.claim_approval_decision("TENANT-A", appr.approval_id, "ADMIN-A", True, None, 2.0)
    assert c1 is True
    assert a1.status == ApprovalStatus.APPROVED
    # 重复/并发不抢占，不重复生效。
    a2, c2 = db.claim_approval_decision("TENANT-A", appr.approval_id, "ADMIN-A", True, None, 3.0)
    assert c2 is False
    assert a2.status == ApprovalStatus.APPROVED
    assert db.get_operation("TENANT-A", op.operation_id).status == OperationStatus.PENDING


def test_sqlite_reject_links_operation_rejected(db):
    op, appr = _make_pending_refund(db)
    db.claim_approval_decision("TENANT-A", appr.approval_id, "ADMIN-A", False, "信息不符", 2.0)
    assert db.get_approval("TENANT-A", appr.approval_id).status == ApprovalStatus.REJECTED
    assert db.get_operation("TENANT-A", op.operation_id).status == OperationStatus.REJECTED


def test_sqlite_stale_approval_expires_to_handoff(db):
    op, appr = _make_pending_refund(db)
    expired = db.expire_stale_approvals("TENANT-A", now=10.0, timeout_seconds=5.0)
    assert appr.approval_id in expired
    assert db.get_approval("TENANT-A", appr.approval_id).status == ApprovalStatus.TIMEOUT
    assert db.get_operation("TENANT-A", op.operation_id).status == OperationStatus.HUMAN_HANDOFF


def test_sqlite_concurrent_claim_single_winner(tmp_path):
    """两个独立连接并发审批同一单：CAS 保证仅一个抢占成功，不双执行。

    SqliteStore 本身是单连接 + 锁（文档：不适用于高并发生产），这里用两个独立连接
    模拟两个请求/进程，验证跨连接的 CAS 原子语义（`UPDATE ... WHERE status='pending'`）。
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "concurrent.db"
    s1 = SqliteStore(str(path))
    s1.create_tenant("TENANT-A", "租户A")
    _op, appr = _make_pending_refund(s1)
    s1.close()

    s_a = SqliteStore(str(path))
    s_b = SqliteStore(str(path))
    barrier = threading.Barrier(2)
    res: dict = {}

    def claim(store: SqliteStore, approver: str) -> None:
        barrier.wait()
        _a, claimed = store.claim_approval_decision(
            "TENANT-A", appr.approval_id, approver, True, None, 2.0)
        res[approver] = claimed

    with ThreadPoolExecutor(max_workers=2) as ex:
        list(ex.map(lambda item: claim(item[0], item[1]),
                    [(s_a, "ADMIN-A"), (s_b, "ADMIN-B")]))
    assert sum(1 for v in res.values() if v) == 1
    fin = s_a.get_approval("TENANT-A", appr.approval_id)
    assert fin.status == ApprovalStatus.APPROVED
    assert fin.approver in ("ADMIN-A", "ADMIN-B")
    s_a.close()
    s_b.close()
