"""会话保留清理测试（claim → adelete_thread → finish；失败重试）。

使用假 store / checkpointer，验证清理工作流与失败重试（无需 PG；PG 集成见
test_pg_checkpoint_recovery）。
"""
from __future__ import annotations

import asyncio
import pytest

from src.infrastructure.session_retention import cleanup_expired_threads


class FakeScopeStore:
    def __init__(self, candidates):
        self._candidates = candidates
        self.claimed = 0
        self.finished = []
        self.failed = []

    def claim_expired_scopes_for_cleanup(self, now, batch_size=100, engine=None):
        self.claimed = len(self._candidates)
        return list(self._candidates)

    def finish_cleanup_scope(self, tenant_id, thread_id, now):
        self.finished.append((tenant_id, thread_id))

    def record_cleanup_failure(self, tenant_id, thread_id, exc, now):
        self.failed.append((tenant_id, thread_id, str(exc)))


class DeletingCheckpointer:
    def __init__(self, fail_threads=()):
        self.fail_threads = set(fail_threads)
        self.deleted = []

    async def adelete_thread(self, thread_id):
        if thread_id in self.fail_threads:
            raise RuntimeError(f"delete failed for {thread_id}")
        self.deleted.append(thread_id)


def test_cleanup_deletes_successfully():
    # adelete_thread 成功后才会删除 scope/session。
    store = FakeScopeStore([{"tenant_id": "T", "thread_id": "th-1", "expires_at": 1}])
    cp = DeletingCheckpointer()
    result = asyncio.run(cleanup_expired_threads(store, cp, now=10, batch_size=10))
    assert result.deleted == 1
    assert result.failed == 0
    assert cp.deleted == ["th-1"]
    assert store.finished == [("T", "th-1")]
    assert store.failed == []


def test_cleanup_failure_retains_scope_and_retries():
    # adelete_thread 失败：不得删 scope/session；保留 deleting 并记录失败（下轮重试）。
    store = FakeScopeStore([{"tenant_id": "T", "thread_id": "th-bad", "expires_at": 1}])
    cp = DeletingCheckpointer(fail_threads=("th-bad",))
    result = asyncio.run(cleanup_expired_threads(store, cp, now=10, batch_size=10))
    assert result.deleted == 0
    assert result.failed == 1
    assert store.finished == []          # 未删 scope/session
    assert store.failed[0][1] == "th-bad"  # 记录了失败（保留 deleting scope，可幂等重试）
    assert cp.deleted == []


def test_cleanup_idempotent_on_empty():
    store = FakeScopeStore([])
    cp = DeletingCheckpointer()
    result = asyncio.run(cleanup_expired_threads(store, cp, now=10, batch_size=10))
    assert result.claimed == 0 and result.deleted == 0 and result.failed == 0
