"""Celery 应用（异步任务：长期记忆写入、批处理重试、会话清理、死信等）。

完全自托管：Celery + Redis Broker（全 Python 技术栈），不采用 BullMQ/Node。

安全要求：
- 任务 payload 一律带 `tenant_id`；任务执行**第一阶段先用 `require_tenant_active`
  复核租户状态**，租户停用/成员无效时拒绝消费（绝不跨租户或无租户执行）。
- 会话清理任务是系统级任务（无单一租户），但它内部按每个租户的 scope 处理，
  且对每个 thread 使用受控 `cleanup` scope（不暴露默认租户）。

实际 Redis 可用性与 worker 运行需 Docker/预发布环境验收；未验证前不宣称可运行。
"""
from __future__ import annotations

from celery import Celery
from celery.signals import worker_init

from src.config import get_settings
from src.infrastructure.store import build_store
from src.tasks.tenant import require_tenant_active

settings = get_settings()

celery_app = Celery(
    "after_sales",
    broker=settings.celery_broker_url,
    backend=settings.celery_broker_url,
)
celery_app.conf.task_default_queue = "after_sales"


@worker_init.connect
def _configure_worker_logging(**_kwargs):
    """Worker 进程启动时接管日志（全局 PII 脱敏 + 可选 JSON 结构化）。

    用信号而非模块顶层，避免测试导入 `src.tasks.worker` 时清空 root handler。
    地址/支付/密钥绝不进明文日志；任务日志带 `tenant_id` 上下文可追踪。
    """
    from src.observability.logging import configure_logging
    configure_logging(level=settings.log_level,
                      json_format=settings.log_json_format, service="worker")

# 进程内缓存的 store（按 settings 构建；postgres 后端为 PostgresStore）。
_store = None


def _get_store():
    global _store
    if _store is None:
        _store = build_store(settings)
    return _store


@celery_app.task(name="memory.write_tick", bind=True, max_retries=3)
def memory_write_tick(self, payload: dict) -> str:
    """占位任务：真正的长期记忆写入（Graphiti+Neo4j）属后续阶段。

    消费前先复核租户状态；payload 必须携带租户作用域。
    """
    from src.tasks.tenant import task_payload

    tenant_id = payload.get("tenant_id") if isinstance(payload, dict) else None
    data = task_payload(tenant_id)  # 缺失租户作用域 → DomainError（默认拒绝）。
    require_tenant_active(_get_store(), data["tenant_id"])  # 消费前复核，停用拒绝。
    return "tick"


@celery_app.task(name="execution.reconcile_tenant", bind=True, max_retries=3, acks_late=True)
def reconcile_executions(self, tenant_id: str) -> str:
    """执行链对账：对指定租户的非终态执行记录向外部网关核实并收口，不一致转人工。"""
    from src.tasks.execution import reconcile_tenant

    r = reconcile_tenant(tenant_id)
    return f"scanned={r['scanned']} reconciled={r['reconciled']} mismatch={r['mis_matched']} handoff={r['human_handoff']}"


@celery_app.task(name="cleanup.expired_sessions", bind=True, max_retries=5, acks_late=True)
def cleanup_expired_sessions(self, batch_size: int = 100) -> str:
    """系统级会话清理（无单一租户，按每个 scope 的租户处理）。

    若存储后端未提供清理条件（如第 1 阶段无 checkpoint），本任务为幂等 no-op，
    真正删除依赖 PostgreSQL 的 checkpoint/scope；本机未装 PG 时仅占位。
    """
    store = _get_store()
    if not hasattr(store, "claim_expired_scopes_for_cleanup"):
        return "noop:no_scopes"
    import asyncio

    from src.infrastructure.session_retention import cleanup_expired_threads

    checkpointer = getattr(store, "_checkpointer", None)
    if checkpointer is None:
        return "noop:no_checkpointer"
    result = asyncio.run(cleanup_expired_threads(store, checkpointer, batch_size=batch_size))
    return f"claimed={result.claimed} deleted={result.deleted} failed={result.failed}"
