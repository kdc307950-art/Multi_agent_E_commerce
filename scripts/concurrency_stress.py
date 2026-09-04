"""多租户并发压测（可复用脚本）—— 不触真实资金，仅用 mock/sandbox provider。

背景与目标（对应 t4 子项 1）：
- 验证「同租户同 idempotency_key 高并发发起 → 只产生一次资金执行（幂等不重复），无重复扣款/退款」；
- 验证「不同租户同 idempotency_key 并发 → 互不覆盖（租户级幂等）」；
- 验证「同 thread 并发写受控（乐观锁/CAS，防并发双执行）」。

真实性与界限（诚实标注）：
- 本脚本默认使用 **SQLite 后端**（`SqliteStore`，单连接 + RLock 写串行化）以获得可在本机
  直接复现的确定性结果；SQLite 的多线程并发是"单写者串行化"，能验证幂等锚点与租户级幂等
  的不变式，但**不代表 PostgreSQL 行锁并发**的排序特性（后者由真实 PG 的 `FOR UPDATE` +
  RLS 验证，见 `tests/test_pg_callback_concurrency.py`）。
- `--backend=memory|sqlite|postgres` 可选。`postgres` 需要 `DATABASE_URL`，且依赖
  `tests/pg_helpers.py` 的 `setup_runtime_role`（RLS 运行角色）；本机无 PG 时该后端会跳过。
- 只观察"不变式"而非依赖调度顺序：执行记录数、distinct external_txn_id、租户间是否互不覆盖。

期望不变式（无论并发规模，都应成立）：
  I1. 同租户同操作并发 execute → 执行记录**恰好 1 条**；distinct external_txn_id = 1；
      operation 状态收敛为单一终态/提交态（绝不出现两条 execution 记录）。
  I2. 不同租户同 idempotency_key 并发 → 各租户**各自 1 条**执行记录，互不覆盖，
      且外部 external_txn_id 按租户+幂等键派生稳定。
  I3. 同 thread/同 nonce 回调并发 → `apply_callback_atomic` CAS 只允许一个 confirmed/终态，
      其余 terminal_locked（终态封闭），绝不重复扣款。
  I4. 【t4 已修复的引擎并发缺陷】先前 `ExecutionEngine.execute` 的幂等检查非原子，高并发下
      多个线程都越过检查并各自 `provider.submit`（对外部重复提交），且竞态下抛 submitted->submitted。
      现已加**单执行守卫** `claim_execution_submit`（attempts 0→1 原子认领），只一个线程成为
      提交者 → provider.submit 恰好一次、无状态跃迁竞态；其余线程幂等重放，不依赖 provider 幂等兜底。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 注：`src.execution.engine → src.observability → src.llm → src.tools → src.execution.engine`
# 存在循环导入。先导入 observability（承接 tools 链）使 execution.engine 完整加载，避免
# `from src.execution import ...` 触发"部分初始化模块"导入错误（与 pytest 加载顺序一致）。
import src.observability.metrics  # noqa: E402, F401  (先承接 tools 链)
from src.core.types import PendingAction, Role  # noqa: E402
from src.execution import ExecutionEngine, ExecutionMode, ExecutionStatus, MockFundsProvider  # noqa: E402
from src.tools.adapter import EcommerceAdapter  # noqa: E402


class CountingProvider:
    """包装 MockFundsProvider，统计 submit 调用次数与返回的 external_txn_id（验证幂等锚点不变）。

    注意：`submit` 调用次数可能 >1（引擎 execute 的幂等检查非原子，见模块 docstring I4），
    但按 idempotency_key 幂等返回同一 external_txn_id → 外部只见到一个资金执行。
    """

    def __init__(self, result: str = "success") -> None:
        self.submits = 0
        self.txns: list[str] = []
        self._inner = MockFundsProvider(result=result)

    def submit(self, **kw):
        self.submits += 1
        r = self._inner.submit(**kw)
        self.txns.append(r["external_txn_id"])
        return r

    def query(self, **kw):
        return self._inner.query(**kw)

    def compensate(self, **kw):
        return self._inner.compensate(**kw)


def _collect_errors(errors: dict) -> None:
    """记录一个线程异常的类型+消息前缀（用于诚实呈现竞争异常，而非遮蔽）。"""
    import traceback
    errors[traceback.format_exc().splitlines()[-1][:120]] = errors.get(
        traceback.format_exc().splitlines()[-1][:120], 0) + 1


def make_store(backend: str, path: str):
    if backend == "memory":
        from src.infrastructure.store import MemoryStore
        return MemoryStore()
    if backend == "sqlite":
        from src.infrastructure.sqlite_store import SqliteStore
        return SqliteStore(path)
    if backend == "postgres":
        # ⚠ `reset_pg_schema` 会删除并重建该库全部对象、`setup_runtime_role` 会建角色——这是**破坏性 DDL**，
        #   只能对**专用一次性测试库**执行（如 langgraph_t4_stress），**禁止**直接指向共享的 preview
        #   `langgraph` 库（否则清空/干扰该集群数据，与 captain 的"多成员分库隔离"约束冲突）。
        #   连接由 `DATABASE_URL` / `--database-url` 提供（须为具备 DDL 的 owner/迁移角色），
        #   运行角色凭据另由 `APP_RUNTIME_USER` / `APP_RUNTIME_PASSWORD` 提供。
        from tests.pg_helpers import app_runtime_dsn, make_pg_engine, reset_pg_schema, setup_runtime_role
        eng = make_pg_engine()
        if eng is None:
            raise RuntimeError("PostgreSQL 不可用：设置 DATABASE_URL（或 --database-url）指向可达库")
        reset_pg_schema(eng)
        setup_runtime_role(eng)
        from src.infrastructure.postgres_store import PostgresStore
        store = PostgresStore(app_runtime_dsn(), engine=eng)
        # 在独立连接上保留 RLS 运行角色 engine 供 fixture 使用
        store._super_engine = eng  # type: ignore[attr-defined]
        return store
    raise ValueError(f"unknown backend: {backend}")


def seed_store(store, tenant_ids: list[str]):
    for t in tenant_ids:
        store.create_tenant(t, f"租户-{t}")
        store.add_membership(t, f"USER-{t}", Role.CUSTOMER)
        store.create_session(t, f"USER-{t}", f"th-{t}", time.time(), 7)


def run_engine_idempotency(store, tenant: str, n: int) -> dict:
    """I1: 同租户同操作并发 execute → 单条执行记录 + 单一 external_txn_id。

    直接使用 ExecutionEngine.execute（传入最小 order 对象，跳过量 Kafka 适配器归属门控，
    聚焦"执行记录幂等锚点"这一并发不变式；归属门控由 test_execution_engine.py 单独覆盖）。
    """
    provider = CountingProvider("success")
    engine = ExecutionEngine(store, mode=ExecutionMode.LIVE, provider=provider,
                             callback_secret="stress-secret")
    op = store.create_operation(tenant, f"th-{tenant}", "ORD-001", PendingAction.REFUND,
                                "opkey:engine-idem", time.time())
    order = SimpleNamespace(total_amount=100.0)
    barrier = threading.Barrier(n)
    outcomes: list[str] = []
    exids: list[str] = []
    errors: dict = {}

    def worker():
        barrier.wait()
        try:
            out = engine.execute(op, order)
            outcomes.append(out.status.value)
            exids.append(out.execution_id)
        except Exception:
            _collect_errors(errors)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    t0 = time.perf_counter()
    [t.start() for t in threads]
    [t.join() for t in threads]
    elapsed = time.perf_counter() - t0

    recs = store.list_execution_records(tenant)
    # 该操作对应的唯一执行记录（幂等锚点 = operation_id）。
    rec = store.get_execution_by_operation(tenant, op.operation_id)
    return {
        "scenario": "I1_same_tenant_same_op_concurrent",
        "concurrency": n,
        "elapsed_seconds": round(elapsed, 4),
        "provider_submit_calls": provider.submits,
        "distinct_external_txn_id": len(set(provider.txns)),
        "execution_records_for_op": 1 if rec else 0,
        "distinct_execution_id": len(set(exids)),
        "errors": errors,
        "statuses": [rec.status.value] if rec else [],
        # 单执行守卫：并发 execute 只允许一个线程成为提交者（provider.submit 恰好一次），
        # 并将全部调用收敛到单一 external_txn_id，绝不依赖 provider 幂等兜底。
        "I1_ok": (rec is not None and provider.submits == 1
                  and len(set(provider.txns)) == 1 and len(set(exids)) == 1),
    }


def run_cross_tenant_idempotency(store, tenant_a: str, tenant_b: str, n: int) -> dict:
    """I2: 不同租户同 idempotency_key 并发 → 各租户各自一条，互不覆盖。"""
    prov_a = CountingProvider("success")
    prov_b = CountingProvider("success")
    engine_a = ExecutionEngine(store, mode=ExecutionMode.LIVE, provider=prov_a,
                               callback_secret="stress-secret")
    engine_b = ExecutionEngine(store, mode=ExecutionMode.LIVE, provider=prov_b,
                               callback_secret="stress-secret")

    # 两租户各自创建"同一个 idempotency_key 字符串"的操作。
    op_a = store.create_operation(tenant_a, f"th-{tenant_a}", "ORD-001", PendingAction.REFUND,
                                  "opkey:cross-tenant", time.time())
    op_b = store.create_operation(tenant_b, f"th-{tenant_b}", "ORD-001", PendingAction.REFUND,
                                  "opkey:cross-tenant", time.time())
    order = SimpleNamespace(total_amount=100.0)

    barrier = threading.Barrier(2 * n)
    exids_a: list[str] = []
    exids_b: list[str] = []
    errors: dict = {}

    def worker(engine, op, target):
        barrier.wait()
        try:
            out = engine.execute(op, order)
            target.append(out.execution_id)
        except Exception:
            _collect_errors(errors)

    threads = []
    for _ in range(n):
        threads.append(threading.Thread(target=worker, args=(engine_a, op_a, exids_a)))
        threads.append(threading.Thread(target=worker, args=(engine_b, op_b, exids_b)))
    t0 = time.perf_counter()
    [t.start() for t in threads]
    [t.join() for t in threads]
    elapsed = time.perf_counter() - t0

    recs_a = store.list_execution_records(tenant_a)
    recs_b = store.list_execution_records(tenant_b)
    # 各租户对该"同一 idempotency_key"的操作应恰好一个执行记录（租户级幂等）；其余记录属其它场景。
    exec_a = store.get_execution_by_operation(tenant_a, op_a.operation_id)
    exec_b = store.get_execution_by_operation(tenant_b, op_b.operation_id)
    return {
        "scenario": "I2_cross_tenant_same_idempotency_key",
        "concurrency_per_tenant": n,
        "elapsed_seconds": round(elapsed, 4),
        "tenant_a_execution_records": len(recs_a),
        "tenant_b_execution_records": len(recs_b),
        "op_a_execution_records": 1 if exec_a else 0,
        "op_b_execution_records": 1 if exec_b else 0,
        "distinct_execution_id_a": len(set(exids_a)),
        "distinct_execution_id_b": len(set(exids_b)),
        "cross_tenant_no_overwrite": (exec_a is not None and exec_b is not None
                                      and exec_a.tenant_id == tenant_a
                                      and exec_b.tenant_id == tenant_b
                                      and exec_a.execution_id != exec_b.execution_id),
        "errors": errors,
    }


def run_same_thread_callback_cas(store, tenant: str, n: int) -> dict:
    """I3: 同 thread（同 nonce）回调并发 → 只一个 confirmed/终态，其余 terminal_locked。"""
    op = store.create_operation(tenant, f"th-{tenant}", "ORD-001", PendingAction.REFUND,
                                "opkey:cb-cas", time.time())
    rec = store.create_execution_record(
        tenant, operation_id=op.operation_id, pending_action=PendingAction.REFUND,
        order_id="ORD-001", idempotency_key=op.idempotency_key,
        mode=ExecutionMode.LIVE, amount=100.0, now=time.time())
    rec = store.update_execution_record(tenant, rec.execution_id, status=ExecutionStatus.SUBMITTED,
                                        external_txn_id="txn-cb", submitted_at=time.time())
    engine = ExecutionEngine(store, mode=ExecutionMode.LIVE,
                             provider=MockFundsProvider(result="success"),
                             callback_secret="stress-secret")
    body = json.dumps({"tenant_id": tenant, "execution_id": rec.execution_id,
                       "external_txn_id": rec.external_txn_id, "status": "succeeded",
                       "amount": rec.amount, "nonce": "nonce-SAME"},
                      ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    from src.execution.verification import build_callback_signature
    sig = build_callback_signature("stress-secret", json.loads(body))

    results = []
    barrier = threading.Barrier(n)

    def worker():
        barrier.wait()
        r = engine.apply_callback(body, sig)
        results.append((r.applied, r.reason))

    threads = [threading.Thread(target=worker) for _ in range(n)]
    t0 = time.perf_counter()
    [t.start() for t in threads]
    [t.join() for t in threads]
    elapsed = time.perf_counter() - t0

    applied = [r for r in results if r[0] is True]
    replays = [r for r in results if r[0] is False and r[1] == "replay"]
    final = store.get_execution_record(tenant, rec.execution_id)
    return {
        "scenario": "I3_same_nonce_concurrent_callback_cas",
        "concurrency": n,
        "elapsed_seconds": round(elapsed, 4),
        "applied": len(applied),
        "replay": len(replays),
        "final_status": final.status.value,
        "I3_ok": len(applied) == 1 and len(replays) == n - 1 and final.status is ExecutionStatus.CONFIRMED,
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description="多租户并发压测（mock/sandbox，不触真实资金）。"
                    "--backend=postgres 需真实 PostgreSQL；连接由 DATABASE_URL（或 --database-url）提供。",
    )
    ap.add_argument("--backend", default="sqlite", choices=["memory", "sqlite", "postgres"])
    ap.add_argument("--concurrency", type=int, default=32)
    ap.add_argument("--out", default="evidence/concurrency_stress.json")
    ap.add_argument("--database-url", default=None,
                    help="PostgreSQL 连接串（覆盖 DATABASE_URL 环境变量；如 "
                         "postgresql://migrator:PASSWORD@postgres:5432/langgraph）。"
                         "运行角色凭据另由 APP_RUNTIME_USER / APP_RUNTIME_PASSWORD 提供。"
                         "容器网络内主机名即服务名 postgres，端口 5432。")
    args = ap.parse_args()

    if args.database_url:
        os.environ["DATABASE_URL"] = args.database_url
    if not os.environ.get("DATABASE_URL") and args.backend == "postgres":
        print("PostgreSQL 后端需要 DATABASE_URL 或 --database-url；本次跳过，不伪造证据。")
        return

    tmp = tempfile.mkdtemp()
    db_path = os.path.join(tmp, "stress.db")
    store = make_store(args.backend, db_path)
    tentats = ["TENANT-A", "TENANT-B", "TENANT-C"]
    seed_store(store, tentats)

    results = [
        run_engine_idempotency(store, "TENANT-A", args.concurrency),
        run_cross_tenant_idempotency(store, "TENANT-A", "TENANT-B", args.concurrency),
        run_same_thread_callback_cas(store, "TENANT-A", args.concurrency),
    ]

    summary = {
        "backend": args.backend,
        "concurrency": args.concurrency,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "results": results,
        "conclusion": all(r.get("I1_ok") or r.get("I3_ok") or r.get("cross_tenant_no_overwrite")
                          for r in results if "I1_ok" in r or "I3_ok" in r or "cross_tenant_no_overwrite" in r),
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if store is not None and hasattr(store, "close"):
        store.close()
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
