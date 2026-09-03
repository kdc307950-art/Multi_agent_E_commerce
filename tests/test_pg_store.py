"""PostgresStore 持久化与多租户语义测试（需要真实 PostgreSQL）。"""
from __future__ import annotations

import pytest

from src.core.types import DomainError, PendingAction, Role, TenantStatus, generate_operation_key
from src.infrastructure.postgres_store import PostgresStore


def _seed(pg_store) -> None:
    pg_store.create_tenant("TENANT-A", "租户A")
    pg_store.create_tenant("TENANT-B", "租户B")
    pg_store.add_membership("TENANT-A", "USER-001", Role.CUSTOMER)
    pg_store.add_membership("TENANT-B", "USER-B1", Role.CUSTOMER)


@pytest.mark.postgres
def test_persists_across_store_rebuild(pg_store, pg_engine):
    # 写入 → 重建 store（同 DB）→ 数据仍在（API 重启不丢失）。
    _seed(pg_store)
    pg_store.create_session("TENANT-A", "USER-001", "th-1", 1000.0, 7)
    key = generate_operation_key(PendingAction.REFUND, "TENANT-A", "ORD-001", "REQ-1")
    op = pg_store.create_operation("TENANT-A", "th-1", "ORD-001", PendingAction.REFUND, key, 1000.0)
    appr = pg_store.create_approval("TENANT-A", "th-1", op.operation_id, PendingAction.REFUND,
                                    "ORD-001", 100.0, "reason", 1000.0)

    store2 = PostgresStore(pg_engine.url.render_as_string(hide_password=False), engine=pg_engine)
    s = store2.get_session("TENANT-A", "th-1")
    assert s.thread_id == "th-1"
    assert store2.get_operation("TENANT-A", op.operation_id).operation_id == op.operation_id
    assert store2.get_approval("TENANT-A", appr.approval_id).approval_id == appr.approval_id
    store2.close()


@pytest.mark.postgres
def test_idempotency_across_connections(pg_store):
    _seed(pg_store)
    # operation 必须归属已存在会话（复合外键 (tenant_id, thread_id) → sessions）。
    pg_store.create_session("TENANT-A", "USER-001", "th-a", 1.0, 7)
    key = generate_operation_key(PendingAction.REFUND, "TENANT-A", "ORD-001", "REQ")
    op1 = pg_store.create_operation("TENANT-A", "th-a", "ORD-001", PendingAction.REFUND, key, 1.0)
    op2 = pg_store.create_operation("TENANT-A", "th-a", "ORD-001", PendingAction.REFUND, key, 2.0)
    assert op1.operation_id == op2.operation_id


@pytest.mark.postgres
def test_cross_tenant_scoped_404(pg_store):
    _seed(pg_store)
    pg_store.create_session("TENANT-A", "USER-001", "th-scope", 1000.0, 7)
    key = generate_operation_key(PendingAction.REFUND, "TENANT-A", "ORD-001", "REQ")
    op = pg_store.create_operation("TENANT-A", "th-scope", "ORD-001", PendingAction.REFUND, key, 1000.0)
    appr = pg_store.create_approval("TENANT-A", "th-scope", op.operation_id, PendingAction.REFUND,
                                    "ORD-001", 100.0, "r", 1000.0)
    # 跨租户读取 → 404（不泄露存在）。
    with pytest.raises(DomainError):
        pg_store.get_operation("TENANT-B", op.operation_id)
    with pytest.raises(DomainError):
        pg_store.get_approval("TENANT-B", appr.approval_id)


@pytest.mark.postgres
def test_suspended_tenant_blocks(pg_store):
    _seed(pg_store)
    pg_store.set_tenant_status("TENANT-A", TenantStatus.SUSPENDED)
    with pytest.raises(DomainError):
        pg_store.create_session("TENANT-A", "USER-001", "th-sus", 1000.0, 7)


@pytest.mark.postgres
def test_checkpoint_scope_created_atomically_with_session(pg_store):
    _seed(pg_store)
    pg_store.create_session("TENANT-A", "USER-001", "th-atomic", 1000.0, 7)
    scope = pg_store.get_checkpoint_scope("TENANT-A", "th-atomic")
    assert scope is not None
    assert scope["status"] == "active"
    # 同一租户同一 thread 只能有一个 scope（UNIQUE(thread_id)），会话重新创建不重复建 scope。
    scope2 = pg_store.get_checkpoint_scope("TENANT-A", "th-atomic")
    assert scope2["thread_id"] == "th-atomic"
