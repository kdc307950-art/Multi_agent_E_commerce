"""Checkpoint 恢复记录证据（Preview 服务器 PostgreSQL，app_runtime 运行角色）。

复现《生产基线与验收测试》验收口径：checkpoint 经锁定版 TenantScopedCheckpointer 持久化，
"重启"（新连接池 + 新 saver）后可恢复；跨租户恢复被拒；过期清理先清 checkpoint 再清 scope/session。
输出 evidence/pg_checkpoint_recovery_record.json。
环境变量：DATABASE_URL（超级用户，仅 reset + 运行角色）、APP_RUNTIME_PASSWORD。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from urllib.parse import urlparse, urlunparse

# Windows 默认 ProactorEventLoop 不支持 psycopg 异步；与 tests.conftest 一致改用 Selector。
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from sqlalchemy import create_engine

from src.core.types import Role
from src.infrastructure import migrations
from src.infrastructure.postgres_store import PostgresStore, _to_sqlalchemy_url
from src.infrastructure.checkpointer import CheckpointRequestScope, checkpoint_request_scope
from src.infrastructure.factory import create_checkpointer, create_postgres_pool
from src.infrastructure.session_retention import cleanup_expired_threads


def _app_runtime_dsn() -> str:
    dsn = os.environ["DATABASE_URL"]
    p = urlparse(dsn)
    host = p.hostname or "127.0.0.1"
    port = f":{p.port}" if p.port else ""
    netloc = f"app_runtime:{os.environ.get('APP_RUNTIME_PASSWORD', 'apppass')}@{host}{port}"
    return urlunparse((p.scheme, netloc, p.path, p.params, p.query, p.fragment))


def _scope(tenant, thread, source):
    return CheckpointRequestScope(tenant_id=tenant, user_id="U", thread_id=thread, source=source)


def build_pool():
    from src.config import get_settings
    s = get_settings()
    s.database_url = _app_runtime_dsn()
    return create_postgres_pool(s)


def build_graph(saver):
    from langgraph.graph import END, START, StateGraph
    from typing_extensions import TypedDict

    class _S(TypedDict):
        v: int

    b = StateGraph(_S)
    b.add_node("set", lambda s: {"v": s.get("v", 0) + 1})
    b.add_edge(START, "set")
    b.add_edge("set", END)
    return b.compile(checkpointer=saver)


async def run() -> dict:
    super_eng = create_engine(_to_sqlalchemy_url(os.environ["DATABASE_URL"]), pool_pre_ping=True)
    migrations.drop_everything(super_eng)
    migrations.initialize_all(super_eng)
    from tests.pg_helpers import setup_runtime_role
    setup_runtime_role(super_eng)
    super_eng.dispose()

    runtime_dsn = _app_runtime_dsn()
    store = PostgresStore(runtime_dsn, engine=create_engine(_to_sqlalchemy_url(runtime_dsn), pool_pre_ping=True))
    store.create_tenant("TENANT-A", "租户A")
    store.add_membership("TENANT-A", "USER-001", Role.CUSTOMER)
    thread_id = store.create_session("TENANT-A", "USER-001", "th-ck-recovery", 1000.0, 7).thread_id

    # ---- 进程 1：写 checkpoint ----
    pool = build_pool(); await pool.open()
    saver = create_checkpointer(pool)
    graph = build_graph(saver)
    cfg = {"configurable": {"thread_id": thread_id}}
    async with checkpoint_request_scope(_scope("TENANT-A", thread_id, "request")):
        await graph.ainvoke({"v": 0}, config=cfg)
    await pool.close()
    written_states = 1

    # ---- 进程 2（重启）：新池 + 新 saver，恢复状态 ----
    pool2 = build_pool(); await pool2.open()
    saver2 = create_checkpointer(pool2)
    graph2 = build_graph(saver2)
    async with checkpoint_request_scope(_scope("TENANT-A", thread_id, "history")):
        snap = await graph2.aget_state(cfg)
    recovered_v = snap.values.get("v")
    await pool2.close()

    # ---- 跨租户恢复拒绝 ----
    from src.core.types import DomainError
    pool3 = build_pool(); await pool3.open()
    saver3 = create_checkpointer(pool3)
    cross_rejected = False
    cross_code = None
    try:
        async with checkpoint_request_scope(_scope("TENANT-B", "other-th", "request")):
            await saver3.aget_tuple({"configurable": {"thread_id": thread_id}})
    except DomainError as e:
        cross_rejected = True
        cross_code = e.code
    await pool3.close()

    # ---- 过期清理：先 checkpoint 后 scope/session ----
    pool4 = build_pool(); await pool4.open()
    saver4 = create_checkpointer(pool4)
    graph4 = build_graph(saver4)
    async with checkpoint_request_scope(_scope("TENANT-A", thread_id, "request")):
        await graph4.ainvoke({"v": 0}, config=cfg)
    claim_engine = create_engine(_to_sqlalchemy_url(os.environ["DATABASE_URL"]), pool_pre_ping=True)
    res = await cleanup_expired_threads(store, saver4, now=999999, claim_engine=claim_engine)
    claim_engine.dispose()
    session_gone = store.get_session_or_none("TENANT-A", thread_id) is None
    scope_gone = store.get_checkpoint_scope("TENANT-A", thread_id) is None
    await pool4.close()
    store.close()

    return {
        "scenario": "TenantScopedCheckpointer restart/cleanup recovery",
        "checkpoint_written": written_states,
        "recovered_state_value_after_restart": recovered_v,
        "expected_recovered_value": 1,
        "cross_tenant_resume_rejected": cross_rejected,
        "cross_tenant_reject_code": cross_code,
        "cleanup_expired_deleted": res.deleted,
        "cleanup_expired_failed": res.failed,
        "cleanup_session_removed": session_gone,
        "cleanup_scope_removed": scope_gone,
        "conclusion": (recovered_v == 1 and written_states == 1 and cross_rejected and cross_code
                       == "cross_tenant_denied" and res.deleted == 1 and res.failed == 0
                       and session_gone and scope_gone),
    }


def main() -> None:
    res = asyncio.run(run())
    res["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    res["pg_server"] = os.environ["DATABASE_URL"].split("@")[-1]
    os.makedirs("evidence", exist_ok=True)
    with open("evidence/pg_checkpoint_recovery_record.json", "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
