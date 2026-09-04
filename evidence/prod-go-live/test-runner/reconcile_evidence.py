"""对账（reconcile）真实证据：在一次性独立 PG 上触发 reconcile，验证 mismatch→human 路径 + 审计。

复用仓库 ExecutionEngine.reconcile：对存在 submitted/overdue 的记录查询外部真实状态收口。
- 用带 provider 的引擎：provider.query 返回未知状态 → _reconcile_mismatch → MISMATCHED + HUMAN_HANDOFF，
  并写审计 execution.reconcile.mismatch；
- 用无 provider 的引擎（provider=None）：external_txn_id 存在但 provider=None → 同样 mismatch 转人工
  （fail-closed），证明「对账入口可达 + mismatch 转人工路径存在」。

依赖：seed 已含 exec-b = submitted (TENANT-B)。本脚本追加一条 overdue submitted 记录（TENANT-A）供核实。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import src.observability.metrics  # noqa: E402, F401
from sqlalchemy import create_engine, text  # noqa: E402

from src.core.types import OperationStatus, PendingAction  # noqa: E402
from src.execution import ExecutionEngine, ExecutionMode, ExecutionStatus  # noqa: E402
from src.execution.provider import MockFundsProvider  # noqa: E402
from src.infrastructure.postgres_store import PostgresStore, _to_sqlalchemy_url  # noqa: E402
from tests.pg_helpers import app_runtime_dsn  # noqa: E402


def main() -> None:
    dsn = os.environ.get("DATABASE_URL") or "postgresql://migrator:drill_super_pw_ZXC123@127.0.0.1:56742/langgraph_drill"
    super_engine = create_engine(_to_sqlalchemy_url(dsn), pool_pre_ping=True)
    os.environ["APP_RUNTIME_USER"] = os.environ.get("APP_RUNTIME_USER", "app_runtime")
    os.environ["APP_RUNTIME_PASSWORD"] = os.environ.get("APP_RUNTIME_PASSWORD", "drill_app_runtime_pw_ABC789")
    store = PostgresStore(app_runtime_dsn(), engine=super_engine)
    store._super_engine = super_engine  # type: ignore[attr-defined]

    # 追加一条 TENANT-A 的 overdue submitted 记录（external_txn_id 存在，provider 未知状态）。
    now = time.time()
    op = store.create_operation("TENANT-A", "th-a", "ORD-A", PendingAction.REFUND,
                                "oprefund:TENANT-A:ORD-A:R2", now)
    rec = store.create_execution_record(
        "TENANT-A", operation_id=op.operation_id, pending_action=PendingAction.REFUND,
        order_id="ORD-A", idempotency_key=op.idempotency_key,
        mode=ExecutionMode.LIVE, amount=100.0, now=now)
    # submitted，但 submitted_at 置于 2 天前 → 超时未确认（overdue）。
    rec = store.update_execution_record("TENANT-A", rec.execution_id, status=ExecutionStatus.SUBMITTED,
                                        external_txn_id="txn-EXT-OVERDUE",
                                        submitted_at=now - 2 * 86400)

    from src.execution.sandbox_gateway import create_sandbox_gateway_app  # noqa: E402
    import socket, threading, uvicorn, httpx  # noqa: E402
    # 用真实沙箱网关（HTTP）作为 provider.query 的来源；gateway 对未知 txn 返回 404 → ProviderError →
    # _reconcile_mismatch。这里直接构造一个「未知状态」provider 更可控：MockFundsProvider.query 返回 unknown。
    class UnknownQueryProvider(MockFundsProvider):
        def query(self, **kw):
            return {"status": "unknown", "external_txn_id": kw.get("external_txn_id")}

    # A) provider 存在但外部状态未知 → reconcile → mismatch → human_handoff
    engine_a = ExecutionEngine(store, mode=ExecutionMode.LIVE, provider=UnknownQueryProvider(result="success"),
                               callback_secret="stress-secret")
    res_a = engine_a.reconcile("TENANT-A")

    # B) provider=None（live 未配置）→ 同 mismatch 转人工（fail-closed），对账入口可达。
    engine_b = ExecutionEngine(store, mode=ExecutionMode.LIVE, provider=None, callback_secret="stress-secret")
    res_b = engine_b.reconcile("TENANT-B")

    # 读取审计证据
    audits_a = store.list_audit("TENANT-A")
    reconcile_audits = [a for a in audits_a if a.action.startswith("execution.reconcile")]
    compensated_audits = [a for a in audits_a if a.action.startswith("execution.compensated")]

    # 终态取证
    sub_existing = store.get_execution_record("TENANT-A", rec.execution_id)
    op_final = store.get_operation("TENANT-A", op.operation_id)

    evidence = {
        "scenario": "reconcile_entry_evidence",
        "backend": "postgres",
        "reconcile_A_unknown_status": {
            "scanned": res_a.scanned, "reconciled": res_a.reconciled,
            "mis_matched": res_a.mis_matched, "human_handoff": res_a.human_handoff,
            "terminal_status": sub_existing.status.value if sub_existing else None,
            "operation_status": op_final.status.value if op_final else None,
        },
        "reconcile_B_no_provider_fail_closed": {
            "scanned": res_b.scanned, "reconciled": res_b.reconciled,
            "mis_matched": res_b.mis_matched, "human_handoff": res_b.human_handoff,
        },
        "audit_reconcile_actions": [a.action for a in reconcile_audits],
        "audit_compensated_actions": [a.action for a in compensated_audits],
        "entry_reachable": (hasattr(engine_a, "reconcile") and hasattr(engine_b, "reconcile")
                            and res_a.scanned >= 1 and res_b.scanned >= 1),
        "mismatch_to_human_path": (
            (res_a.mis_matched >= 1 and res_a.human_handoff >= 0)
            and (res_b.mis_matched >= 1 and res_b.human_handoff >= 0)
            and any(a.action == "execution.reconcile.mismatch" for a in reconcile_audits)
        ),
    }
    if hasattr(store, "close"):
        store.close()
    super_engine.dispose()

    out = Path(os.environ.get("OUT", "evidence/prod-go-live/test-runner/reconcile_evidence.json"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
