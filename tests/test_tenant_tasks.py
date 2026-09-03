"""Celery/Redis 任务租户绑定与消费前复核测试（无外部依赖）。"""
from __future__ import annotations

import pytest

from src.core.types import DomainError, Role, TenantStatus
from src.infrastructure.store import MemoryStore
from src.tasks.tenant import enqueue_tenant_task, require_tenant_active, task_payload


@pytest.fixture
def store() -> MemoryStore:
    s = MemoryStore()
    s.create_tenant("TENANT-A", "租户A")
    s.create_tenant("TENANT-B", "租户B")
    s.add_membership("TENANT-A", "USER-001", Role.CUSTOMER)
    return s


def test_task_payload_injects_tenant():
    payload = task_payload("TENANT-A", order_id="ORD-1")
    assert payload["tenant_id"] == "TENANT-A"
    assert payload["order_id"] == "ORD-1"


def test_task_payload_rejects_missing_tenant():
    with pytest.raises(DomainError):
        task_payload(None)
    with pytest.raises(DomainError):
        task_payload("")


def test_require_tenant_active_ok(store):
    tenant = require_tenant_active(store, "TENANT-A")
    assert tenant.id == "TENANT-A"


def test_require_tenant_active_blocks_suspended(store):
    store.set_tenant_status("TENANT-A", TenantStatus.SUSPENDED)
    with pytest.raises(DomainError):
        require_tenant_active(store, "TENANT-A")


def test_require_tenant_active_checks_membership(store):
    with pytest.raises(DomainError):
        require_tenant_active(store, "TENANT-A", user_id="NOPE")


def test_require_tenant_active_missing_tenant(store):
    with pytest.raises(DomainError):
        require_tenant_active(store, "TENANT-X")


class _FakeApp:
    def __init__(self):
        self.sent = []

    def send_task(self, task_name, args=None, kwargs=None, countdown=0):
        self.sent.append({"task": task_name, "args": args, "kwargs": kwargs,
                          "countdown": countdown})
        return "task-id"


def test_enqueue_tenant_task_binds_tenant_to_payload():
    app = _FakeApp()
    enqueue_tenant_task(app, "memory.write_tick", "TENANT-A", order_id="ORD-9")
    assert app.sent[0]["task"] == "memory.write_tick"
    assert app.sent[0]["args"][0]["tenant_id"] == "TENANT-A"
    assert app.sent[0]["args"][0]["order_id"] == "ORD-9"
