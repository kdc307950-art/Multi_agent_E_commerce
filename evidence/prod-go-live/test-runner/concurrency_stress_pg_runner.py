"""真实 PostgreSQL（RLS + 行锁）并发压测 —— 复用 concurrency_stress.py 的不变式函数。

与 `scripts/concurrency_stress.py --backend postgres` 的差异：那一条路径会调用
`tests/pg_helpers.reset_pg_schema`（会 drop 后按仓库迁移语句重建 schema）——但仓库迁移的
`shipping_events` FK DDL 有缺陷（见 init_drill_pg.py 说明），干净库重建必失败。因此本脚本
**不复位、复用已初始化（含修正 FK + RLS + app_runtime/backup_role）的一次性独立库**，仅做
seed 后跑真实 PG 上的并发不变式，验证 RLS/行锁/幂等锚点在真实 PostgreSQL 上成立。

三种后端不变式（与 concurrency_stress.py 完全一致）：
  I1 同租户同操作并发 execute → submit 恰好 1 次、同 external_txn_id、record=1、无 errors。
  I2 不同租户同 idempotency_key 并发 → 各租户各自 1 条，互不覆盖（租户级幂等 + RLS）。
  I3 同 thread 同 nonce 回调并发 → CAS 只允许 1 个 confirmed，其余 terminal_locked。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import src.observability.metrics  # noqa: E402, F401
from sqlalchemy import create_engine  # noqa: E402

from src.core.types import PendingAction, Role  # noqa: E402
from src.execution import ExecutionEngine, ExecutionMode, ExecutionStatus, MockFundsProvider  # noqa: E402
from src.execution.verification import build_callback_signature  # noqa: E402
from src.infrastructure.postgres_store import PostgresStore, _to_sqlalchemy_url  # noqa: E402

# 复用 concurrency_stress.py 的 CountingProvider 与不变式函数
from scripts.concurrency_stress import CountingProvider, _collect_errors, run_engine_idempotency, run_cross_tenant_idempotency, run_same_thread_callback_cas  # noqa: E402


def main() -> None:
    dsn = os.environ.get("DATABASE_URL") or "postgresql://migrator:drill_super_pw_ZXC123@127.0.0.1:56742/langgraph_drill"
    super_engine = create_engine(_to_sqlalchemy_url(dsn), pool_pre_ping=True)

    # 运行角色连接（RLS 作用域；口令由 init 时设置）
    from tests.pg_helpers import app_runtime_dsn
    os.environ["APP_RUNTIME_USER"] = os.environ.get("APP_RUNTIME_USER", "app_runtime")
    os.environ["APP_RUNTIME_PASSWORD"] = os.environ.get("APP_RUNTIME_PASSWORD", "drill_app_runtime_pw_ABC789")
    rdsn = app_runtime_dsn()
    store = PostgresStore(rdsn, engine=super_engine)
    store._super_engine = super_engine  # type: ignore[attr-defined]

    # 清理先前运行残留（一次性独立库，仅限本演练租户），避免 create_tenant 幂等冲突。
    from sqlalchemy import text
    with super_engine.begin() as conn:
        for t in ("TENANT-A", "TENANT-B", "TENANT-C"):
            conn.execute(text("DELETE FROM audit WHERE tenant_id=:t"), {"t": t})
            conn.execute(text("DELETE FROM shipping_events WHERE tenant_id=:t"), {"t": t})
            conn.execute(text("DELETE FROM orders WHERE tenant_id=:t"), {"t": t})
            conn.execute(text("DELETE FROM checkpoint_thread_scopes WHERE tenant_id=:t"), {"t": t})
            conn.execute(text("DELETE FROM stream_events WHERE tenant_id=:t"), {"t": t})
            conn.execute(text("DELETE FROM streams WHERE tenant_id=:t"), {"t": t})
            conn.execute(text("DELETE FROM executions WHERE tenant_id=:t"), {"t": t})
            conn.execute(text("DELETE FROM approvals WHERE tenant_id=:t"), {"t": t})
            conn.execute(text("DELETE FROM operations WHERE tenant_id=:t"), {"t": t})
            conn.execute(text("DELETE FROM sessions WHERE tenant_id=:t"), {"t": t})
            conn.execute(text("DELETE FROM memberships WHERE tenant_id=:t"), {"t": t})
            conn.execute(text("DELETE FROM tenants WHERE id=:t"), {"t": t})

    for t in ("TENANT-A", "TENANT-B", "TENANT-C"):
        store.create_tenant(t, f"租户-{t}")
        store.add_membership(t, f"USER-{t}", Role.CUSTOMER)
        store.create_session(t, f"USER-{t}", f"th-{t}", time.time(), 7)

    n = 256
    results = [
        run_engine_idempotency(store, "TENANT-A", n),
        run_cross_tenant_idempotency(store, "TENANT-A", "TENANT-B", n),
        run_same_thread_callback_cas(store, "TENANT-A", n),
    ]
    if hasattr(store, "close"):
        store.close()
    super_engine.dispose()

    out = Path(os.environ.get("OUT", "evidence/prod-go-live/test-runner/concurrency_stress_pg.json"))
    summary = {
        "backend": "postgres",
        "concurrency": n,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "note": "真实 PostgreSQL（RLS + 行锁）并发不变式；不复位共享 schema（一次性独立库），仅 seed 后跑。",
        "results": results,
        "conclusion": all(r.get("I1_ok") or r.get("I3_ok") or r.get("cross_tenant_no_overwrite")
                          for r in results if "I1_ok" in r or "I3_ok" in r or "cross_tenant_no_overwrite" in r),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
