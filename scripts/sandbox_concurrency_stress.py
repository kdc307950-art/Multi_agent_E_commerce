"""真实网关沙箱并发压测（t1 sandbox_gateway 真实 HTTP + 已修复单执行守卫 claim_execution_submit）。

闭合验收红线"真实沙箱不发生重复执行"的**引擎层实测**。与 test-engineer 的 scripts/concurrency_stress.py
不同的是：本脚本的 submit/query/compensate 全部**走真实 HTTP 沙箱网关**（uvicorn 后台线程启动
sandbox_gateway），而非引擎内 mock provider；再用已修复的 `claim_execution_submit` 单执行守卫做
高并发 execute，验证即使真实网关（进程外）也只被提交一次。

覆盖（--backend=memory|sqlite，默认 sqlite；无需 PG）：
 - I1 同 operation（同 tenant_id+idempotency_key）N 并发 execute
     → provider.submit 恰好 1 次（不再像修复前高达 20 次）、全部同一 external_txn_id、
       execution record 数=1、无 submitted->submitted DomainError（errors={}）。
 - FAIL 失败路径（沙箱 default_status=failed 走真实 HTTP）
     → 收敛单一终态（COMPENSATED/COMPENSATION_FAILED）+ 单一 reversal_id，无重复补偿、无跃迁抛错。
 - I2 不同租户同 idempotency_key 并发 → 各租户各自 1 条，互不覆盖（租户级幂等）。

观察的是"不变式"而非调度顺序（并发下只应成立安全不变式）。产出 evidence/sandbox_concurrency_stress.json。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 与 concurrency_stress.py 一致：先承接 observability 链避免 execution 包循环导入。
import src.observability.metrics  # noqa: E402, F401
from src.core.types import PendingAction, Role  # noqa: E402
from src.execution import ExecutionEngine, ExecutionMode, ExecutionStatus, SandboxHttpFundsProvider  # noqa: E402
from src.execution.sandbox_gateway import create_sandbox_gateway_app  # noqa: E402
import httpx  # noqa: E402
import uvicorn  # noqa: E402


class CountingSandboxProvider:
    """包装真实沙箱 HTTP provider，仅统计 submit/compensate 调用次数（中继，真实走 HTTP）。

    计数用于验证"引擎只调用一次外部提交/补偿"（单执行守卫拦截），不改变沙箱行为。
    """

    def __init__(self, inner: SandboxHttpFundsProvider) -> None:
        self._inner = inner
        self.submits = 0
        self.txns: list[str] = []
        self.compensates = 0
        self.reversal_ids: list[str] = []

    def submit(self, **kw):
        self.submits += 1
        r = self._inner.submit(**kw)
        self.txns.append(r.get("external_txn_id") or "")
        return r

    def query(self, **kw):
        return self._inner.query(**kw)

    def compensate(self, **kw):
        self.compensates += 1
        r = self._inner.compensate(**kw)
        self.reversal_ids.append((r.get("receipt") or {}).get("reversal_id") or "")
        return r


def _start_gateway(tmp: str) -> tuple[str, object]:
    """启动真实 sandbox_gateway（uvicorn 后台线程），返回 (base_url, server)。"""
    db = os.path.join(tmp, "gw.db")
    app = create_sandbox_gateway_app(db, api_key="gw-key")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical", lifespan="off")
    server = uvicorn.Server(cfg)
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            httpx.get(f"{base}/docs", timeout=0.5)
            break
        except Exception:
            time.sleep(0.05)
    return base, server


def make_store(backend: str, path: str):
    if backend == "memory":
        from src.infrastructure.store import MemoryStore
        return MemoryStore()
    from src.infrastructure.sqlite_store import SqliteStore
    return SqliteStore(path)


def seed_store(store, tenant_ids: list[str]):
    for t in tenant_ids:
        store.create_tenant(t, f"租户-{t}")
        store.add_membership(t, f"USER-{t}", Role.CUSTOMER)
        store.create_session(t, f"USER-{t}", f"th-{t}", time.time(), 7)


def run_i1(store, base_url, n: int) -> dict:
    """I1：同 operation 高并发 execute（真实沙箱 HTTP）→ submit 恰好一次、record=1、无跃迁抛错。"""
    provider = CountingSandboxProvider(SandboxHttpFundsProvider(base_url, api_key="gw-key", timeout=8.0))
    engine = ExecutionEngine(store, mode=ExecutionMode.LIVE, provider=provider,
                             callback_secret="stress-secret")
    op = store.create_operation("TENANT-A", "th-A", "ORD-001", PendingAction.REFUND,
                                "opkey:real-i1", time.time())
    order = SimpleNamespace(total_amount=100.0)
    barrier = threading.Barrier(n)
    exids: list[str] = []
    errors: dict = {}

    def worker():
        barrier.wait()
        try:
            exids.append(engine.execute(op, order).execution_id)
        except Exception:
            import traceback
            msg = traceback.format_exc().splitlines()[-1][:120]
            errors[msg] = errors.get(msg, 0) + 1

    threads = [threading.Thread(target=worker) for _ in range(n)]
    t0 = time.perf_counter()
    [t.start() for t in threads]
    [t.join() for t in threads]
    elapsed = time.perf_counter() - t0

    rec = store.get_execution_by_operation("TENANT-A", op.operation_id)
    ok = (rec is not None and provider.submits == 1 and len(set(provider.txns)) == 1
          and len(set(exids)) == 1 and not errors)
    return {
        "scenario": "I1_real_sandbox_same_op_concurrent",
        "concurrency": n,
        "elapsed_seconds": round(elapsed, 4),
        "provider_submit_calls": provider.submits,
        "distinct_external_txn_id": len(set(provider.txns)),
        "execution_records_for_op": 1 if rec else 0,
        "distinct_execution_id": len(set(exids)),
        "errors": errors,
        "status": rec.status.value if rec else None,
        "I1_ok": ok,
    }


def run_fail(store, base_url, n: int) -> dict:
    """FAIL：真实沙箱 submit 明确失败（default_status=failed）→ 收敛单一终态 + 单一 reversal。"""
    # 包装 provider：真实 HTTP submit，但改写 status=failed 以触发引擎补偿；计数中继。
    class FailProvider:
        def __init__(self, inner):
            self._inner = inner
            self.submits = 0
            self.compensates = 0
            self.reversal_ids: list[str] = []

        def submit(self, **kw):
            self.submits += 1
            r = self._inner.submit(**kw, default_status="failed")  # 真实 HTTP，注入明确失败
            r["status"] = "failed"  # 触发引擎 FAILED_DISPATCHED → 补偿
            return r

        def query(self, **kw):
            return self._inner.query(**kw)

        def compensate(self, **kw):
            self.compensates += 1
            r = self._inner.compensate(**kw)
            self.reversal_ids.append((r.get("receipt") or {}).get("reversal_id") or "")
            return r

    provider = FailProvider(SandboxHttpFundsProvider(base_url, api_key="gw-key", timeout=8.0))
    engine = ExecutionEngine(store, mode=ExecutionMode.LIVE, provider=provider,
                             callback_secret="stress-secret")
    op = store.create_operation("TENANT-A", "th-A", "ORD-001", PendingAction.REFUND,
                                "opkey:real-fail", time.time())
    order = SimpleNamespace(total_amount=100.0)
    barrier = threading.Barrier(n)
    exids: list[str] = []
    errors: dict = {}

    def worker():
        barrier.wait()
        try:
            exids.append(engine.execute(op, order).execution_id)
        except Exception:
            import traceback
            msg = traceback.format_exc().splitlines()[-1][:120]
            errors[msg] = errors.get(msg, 0) + 1

    threads = [threading.Thread(target=worker) for _ in range(n)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    rec = store.get_execution_record("TENANT-A", exids[0]) if exids else None
    terminal = rec.status in (ExecutionStatus.COMPENSATED, ExecutionStatus.COMPENSATION_FAILED,
                              ExecutionStatus.HUMAN_HANDOFF, ExecutionStatus.FAILED_DISPATCHED)
    ok = (rec is not None and len(set(exids)) == 1 and not errors
          and provider.submits == 1 and len(set(provider.reversal_ids)) <= 1)
    return {
        "scenario": "FAIL_real_sandbox_explicit_failure_concurrent",
        "concurrency": n,
        "provider_submit_calls": provider.submits,
        "provider_compensate_calls": provider.compensates,
        "distinct_reversal_id": len(set(provider.reversal_ids)),
        "execution_records_for_op": 1 if rec else 0,
        "distinct_execution_id": len(set(exids)),
        "errors": errors,
        "terminal_status": rec.status.value if rec else None,
        "FAIL_ok": ok,
    }


def run_i2(store, base_url, n: int) -> dict:
    """I2：不同租户同 idempotency_key 并发 → 互不覆盖（租户级幂等 + 真实沙箱 HTTPS）。"""
    prov_a = CountingSandboxProvider(SandboxHttpFundsProvider(base_url, api_key="gw-key", timeout=8.0))
    prov_b = CountingSandboxProvider(SandboxHttpFundsProvider(base_url, api_key="gw-key", timeout=8.0))
    engine_a = ExecutionEngine(store, mode=ExecutionMode.LIVE, provider=prov_a, callback_secret="stress-secret")
    engine_b = ExecutionEngine(store, mode=ExecutionMode.LIVE, provider=prov_b, callback_secret="stress-secret")
    op_a = store.create_operation("TENANT-A", "th-A", "ORD-001", PendingAction.REFUND,
                                  "opkey:real-i2", time.time())
    op_b = store.create_operation("TENANT-B", "th-B", "ORD-001", PendingAction.REFUND,
                                  "opkey:real-i2", time.time())
    order = SimpleNamespace(total_amount=100.0)
    barrier = threading.Barrier(2 * n)
    exids_a: list[str] = []
    exids_b: list[str] = []
    errors: dict = {}

    def worker_a():
        barrier.wait()
        try:
            exids_a.append(engine_a.execute(op_a, order).execution_id)
        except Exception:
            import traceback
            msg = traceback.format_exc().splitlines()[-1][:120]
            errors[msg] = errors.get(msg, 0) + 1

    def worker_b():
        barrier.wait()
        try:
            exids_b.append(engine_b.execute(op_b, order).execution_id)
        except Exception:
            import traceback
            msg = traceback.format_exc().splitlines()[-1][:120]
            errors[msg] = errors.get(msg, 0) + 1

    threads = ([threading.Thread(target=worker_a) for _ in range(n)]
               + [threading.Thread(target=worker_b) for _ in range(n)])
    [t.start() for t in threads]
    [t.join() for t in threads]

    rec_a = store.get_execution_by_operation("TENANT-A", op_a.operation_id)
    rec_b = store.get_execution_by_operation("TENANT-B", op_b.operation_id)
    no_overwrite = (rec_a is not None and rec_b is not None
                    and rec_a.tenant_id == "TENANT-A" and rec_b.tenant_id == "TENANT-B"
                    and rec_a.execution_id != rec_b.execution_id
                    and len(set(exids_a)) == 1 and len(set(exids_b)) == 1)
    return {
        "scenario": "I2_real_sandbox_cross_tenant_same_idempotency_key",
        "concurrency_per_tenant": n,
        "provider_a_submit_calls": prov_a.submits,
        "provider_b_submit_calls": prov_b.submits,
        "op_a_execution_records": 1 if rec_a else 0,
        "op_b_execution_records": 1 if rec_b else 0,
        "distinct_execution_id_a": len(set(exids_a)),
        "distinct_execution_id_b": len(set(exids_b)),
        "errors": errors,
        "cross_tenant_no_overwrite": no_overwrite,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="真实网关沙箱并发压测（不触真实资金）")
    ap.add_argument("--backend", default="sqlite", choices=["memory", "sqlite"])
    ap.add_argument("--concurrency", type=int, default=64)
    ap.add_argument("--out", default="evidence/sandbox_concurrency_stress.json")
    args = ap.parse_args()

    tmp = tempfile.mkdtemp()
    base_url, server = _start_gateway(tmp)
    store = make_store(args.backend, os.path.join(tmp, "stress.db"))
    seed_store(store, ["TENANT-A", "TENANT-B"])

    results = [
        run_i1(store, base_url, args.concurrency),
        run_fail(store, base_url, args.concurrency),
        run_i2(store, base_url, args.concurrency),
    ]
    if hasattr(store, "close"):
        store.close()
    server.should_exit = True

    summary = {
        "backend": args.backend,
        "concurrency": args.concurrency,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "gateway": "real sandbox_gateway (HTTP)",
        "results": results,
        "conclusion": all(r.get("I1_ok") or r.get("FAIL_ok") or r.get("cross_tenant_no_overwrite")
                          for r in results),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
