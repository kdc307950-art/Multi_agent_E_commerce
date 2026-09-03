"""PostgreSQL checkpoint 恢复与数据面测试（需要真实 PostgreSQL）。

验证验收标准：checkpoint 经锁定版 AsyncPostgresSaver 持久化并可在"重启"后恢复；
跨租户恢复被拒；删除清理先清 checkpoint 再删 scope/session。
"""
from __future__ import annotations

import pytest
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from src.core.types import Role
from src.infrastructure.checkpointer import (
    CheckpointRequestScope,
    checkpoint_request_scope,
)


class _S(TypedDict):
    v: int


def _seed(store) -> str:
    store.create_tenant("TENANT-A", "租户A")
    store.add_membership("TENANT-A", "USER-001", Role.CUSTOMER)
    return store.create_session("TENANT-A", "USER-001", "th-recovery", 1000.0, 7).thread_id


async def _make_runtime():
    from tests.pg_helpers import app_runtime_dsn

    from src.infrastructure.factory import create_checkpointer, create_postgres_pool

    pool = create_postgres_pool(_pool_settings())
    await pool.open()
    return pool, create_checkpointer(pool)


def _pool_settings():
    from tests.pg_helpers import app_runtime_dsn

    from src.config import get_settings
    s = get_settings()
    s.database_url = app_runtime_dsn()
    return s


def _scope(tenant_id, thread_id, source):
    return CheckpointRequestScope(tenant_id=tenant_id, user_id="U", thread_id=thread_id,
                                  source=source)


def _build_graph(checkpointer):
    builder = StateGraph(_S)
    builder.add_node("set", lambda s: {"v": s.get("v", 0) + 1})
    builder.add_edge(START, "set")
    builder.add_edge("set", END)
    return builder.compile(checkpointer=checkpointer)


@pytest.mark.postgres
async def test_minimal_checkpoint_survives_restart(pg_store):
    thread_id = _seed(pg_store)
    # 第一段进程：写 checkpoint。
    pool, saver = await _make_runtime()
    graph = _build_graph(saver)
    cfg = {"configurable": {"thread_id": thread_id}}
    async with checkpoint_request_scope(_scope("TENANT-A", thread_id, "request")):
        await graph.ainvoke({"v": 0}, config=cfg)
    await pool.close()

    # 第二段进程（重启）：新池 + 新 saver，从 checkpoint 恢复读状态。
    pool2, saver2 = await _make_runtime()
    graph2 = _build_graph(saver2)
    async with checkpoint_request_scope(_scope("TENANT-A", thread_id, "history")):
        snap = await graph2.aget_state(cfg)
    assert snap.values.get("v") == 1
    await pool2.close()


@pytest.mark.postgres
async def test_cross_tenant_checkpoint_resume_rejected(pg_store):
    from src.core.types import DomainError

    thread_id = _seed(pg_store)
    pool, saver = await _make_runtime()
    # 租户 B 作用域读租户 A 的线程 → 可信作用域线程不一致 → 拒绝（不泄露存在）。
    with pytest.raises(DomainError) as ei:
        async with checkpoint_request_scope(_scope("TENANT-B", "other-th", "request")):
            await saver.aget_tuple({"configurable": {"thread_id": thread_id}})
    assert ei.value.code == "cross_tenant_denied"
    await pool.close()


@pytest.mark.postgres
async def test_cleanup_expired_scopes_deletes_checkpoint_first(pg_store, pg_engine):
    from src.infrastructure.session_retention import cleanup_expired_threads

    thread_id = _seed(pg_store)
    pool, saver = await _make_runtime()
    # 先写入一个 checkpoint，再清理（now 远大于 expires_at）。
    graph = _build_graph(saver)
    cfg = {"configurable": {"thread_id": thread_id}}
    async with checkpoint_request_scope(_scope("TENANT-A", thread_id, "request")):
        await graph.ainvoke({"v": 0}, config=cfg)
    # 认领（claim）是系统级跨租户扫描：用可绕过 RLS 的超级用户引擎。
    result = await cleanup_expired_threads(pg_store, saver, now=999999, claim_engine=pg_engine)
    assert result.deleted == 1
    assert result.failed == 0
    assert pg_store.get_session_or_none("TENANT-A", thread_id) is None
    assert pg_store.get_checkpoint_scope("TENANT-A", thread_id) is None
    await pool.close()
