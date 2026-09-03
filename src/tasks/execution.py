"""执行链对账任务（Celery + Redis Broker，与 worker 同一应用）。

安全要求：
- 任务 payload 带 `tenant_id`；执行前用 `require_tenant_active` 复核租户状态（停用拒绝）；
- 对账只在租户作用域内进行；无租户上下文默认拒绝；
- shadow 模式下执行记录已收敛为 confirmed（不触真实资金），因此无遗留对账目标；
  live 模式才对外部网关发起查询并向真实状态收口，不一致转人工。
"""
from __future__ import annotations

from src.config import get_settings
from src.infrastructure.store import build_store
from src.tasks.tenant import require_tenant_active
from src.tools import build_adapter

settings = get_settings()

_store = None
_adapter = None


def _get_store():
    global _store
    if _store is None:
        _store = build_store(settings)
    return _store


def _get_adapter():
    global _adapter
    if _adapter is None:
        _adapter = build_adapter(settings)
    return _adapter


def reconcile_tenant(tenant_id: str) -> dict:
    """确定性对账入口（可被 Celery 任务或直接调用）：对指定租户的非终态执行收口。

    返回 {scanned, reconciled, mis_matched, human_handoff}。live 未开启/无 provider 时，
    reconcile 会把无法核实的记录标记为 mismatch 转人工（fail-closed），绝不静默。
    """
    if not tenant_id:
        raise ValueError("缺少租户作用域")
    require_tenant_active(_get_store(), tenant_id)
    adapter = _get_adapter()
    engine = adapter.make_execution_engine(_get_store())
    result = engine.reconcile(tenant_id)
    return {"scanned": result.scanned, "reconciled": result.reconciled,
            "mis_matched": result.mis_matched, "human_handoff": result.human_handoff}


def reconcile_all_tenants() -> dict:
    """对所有存在非终态执行记录的租户执行对账（系统级，无单一租户）。"""
    store = _get_store()
    # 依赖 store 暴露的租户迭代：Memory/SQLite 无 list_all_tenants，退化为批处理由调用方按租户触发。
    if not hasattr(store, "list_all_tenants"):
        return {"skipped": True, "reason": "store 未提供 system 级租户遍历，转为按租户触发"}
    total = {"scanned": 0, "reconciled": 0, "mis_matched": 0, "human_handoff": 0}
    for tenant_id in store.list_all_tenants():
        try:
            r = reconcile_tenant(tenant_id)
            for k in total:
                total[k] += r.get(k, 0)
        except Exception as exc:  # noqa: BLE001 - 单租户对账失败不阻断其它租户
            total.setdefault("errors", []).append({"tenant_id": tenant_id, "error": str(exc)})
    return total
