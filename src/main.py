"""FastAPI 应用入口。

通过 create_app 工厂构建；默认使用内存存储 + Mock LLM + InMemorySaver（本地开发/测试）。
生产应切换 storage_backend=postgres 与真实 OpenAI 兼容 LLM 端点，其可用性待 Docker/预发布验证。

注意：Phase 1 的 MemoryStore 不跨进程持久化；独立进程间的数据不共享，仅用于本地功能验证。

安全要点：
- 受限环境（preview/production）**禁止 Mock 认证**：`auth_backend != real` 时启动即
  fail-closed（抛 RuntimeError），防止用可伪造的 `mock:*` 令牌对外服务。
- 生产优先"统一反向代理 + 同源 /api"；仅在明确跨域来源时通过 `enable_cors` 开启 CORS 白名单。
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from langgraph.checkpoint.memory import InMemorySaver

from src.api import routes
from src.config import Settings, get_settings
from src.core.types import DomainError, Role
from src.infrastructure.store import MemoryStore, build_store
from src.llm import build_llm
from src.retrieval import build_retriever
from src.service.chat import ChatRunner
from src.tools import build_adapter


def seed_default(store: MemoryStore) -> None:
    """开发/测试种子：两个租户，各自 customer 与 admin/approver，用于演示与隔离测试。"""
    try:
        store.create_tenant("TENANT-A", "租户A")
        store.create_tenant("TENANT-B", "租户B")
    except Exception:
        return  # 已存在则跳过（幂等）

    store.add_membership("TENANT-A", "USER-001", Role.CUSTOMER)
    store.add_membership("TENANT-A", "USER-002", Role.CUSTOMER)
    store.add_membership("TENANT-A", "AGENT-A", Role.AGENT)
    store.add_membership("TENANT-A", "ADMIN-A", Role.ADMIN)
    store.add_membership("TENANT-A", "APPROVER-A", Role.APPROVER)
    store.add_membership("TENANT-B", "USER-B1", Role.CUSTOMER)
    store.add_membership("TENANT-B", "USER-B2", Role.CUSTOMER)
    store.add_membership("TENANT-B", "AGENT-B", Role.AGENT)
    store.add_membership("TENANT-B", "ADMIN-B", Role.ADMIN)
    store.add_membership("TENANT-B", "APPROVER-B", Role.APPROVER)


def fail_closed_auth_guard(settings: Settings) -> None:
    """受限环境（preview/production）必须使用真实认证后端且具备 JWT 密钥，否则启动即失败。

    这是 fail-closed，覆盖两类危险配置，任何一类都禁止对外服务：
    - 受限环境 + Mock 认证：可伪造的 `mock:*` 令牌暴露给公网。
    - 受限环境 + real 后端但缺失 `AUTH_JWT_SECRET`：所有请求将无法校验而拒绝，
      但仍不应允许"带病启动"。启动即失败，避免出现"服务在跑但认证永远 503"的半死不活状态。
    """
    if not settings.is_restricted_env:
        return
    if settings.auth_backend != "real":
        raise RuntimeError(
            "受限环境（preview/production）禁止使用 Mock 认证；"
            "请配置 AUTH_BACKEND=real 与 AUTH_JWT_SECRET。当前启动 fail-closed。"
        )
    if not settings.auth_jwt_secret:
        raise RuntimeError(
            "受限环境（preview/production）且 AUTH_BACKEND=real 时必须配置 AUTH_JWT_SECRET；"
            "当前为空，启动 fail-closed。"
        )


def create_app(store=None, llm=None, checkpointer=None,
               retriever=None, seed: bool = True, settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    fail_closed_auth_guard(settings)
    # 首次上线门控：受限环境下严格校验（少量租户/仅审批后执行/全量审计/人工复核）。
    # 默认宽松（launch_gate_strict=False），上线时由 verify_launch_gate 脚本 --strict 强制执行。
    from src.core.launch_gate import enforce_strict, verify_launch_gate
    launch_gate_report = verify_launch_gate(settings)
    enforce_strict(settings)
    adapter = build_adapter(settings)
    conf_threshold = settings.intent_confidence_threshold

    lifespan = None
    postgres_mode = store is None and settings.storage_backend == "postgres"
    if postgres_mode:
        # Phase 2：PostgreSQL 数据面。PostgresStore 为同步业务面；TenantScopedCheckpointer
        # 依赖 AsyncConnectionPool 且其构造需事件循环，故在 lifespan 内构建并迁移。
        from contextlib import asynccontextmanager

        from src.infrastructure.factory import (
            create_checkpointer,
            create_postgres_pool,
            create_postgres_store,
            initialize_postgres,
            require_postgres_ready,
        )

        store = create_postgres_store(settings)
        pool = create_postgres_pool(settings)
        llm = llm if llm is not None else build_llm(settings)
        retriever = retriever if retriever is not None else build_retriever(settings)
        # 用占位 InMemorySaver 先组 runner（仅用于编译）；真实 saver 在 lifespan 内重建。
        checkpointer = None  # 占位；lifespan 内用真实 TenantScopedCheckpointer 覆盖。
        runner = ChatRunner(store, llm, InMemorySaver(), retriever=retriever,
                            adapter=adapter, conf_threshold=conf_threshold)

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            # 自托管观测：统一接管日志（全局 PII 脱敏 + 可选 JSON 结构化）。此处在受限
            # 环境也配置，确保预览/生产日志带 tenant 上下文且地址/支付/密钥不回明文日志。
            from src.observability.logging import configure_logging
            configure_logging(level=settings.log_level,
                              json_format=settings.log_json_format, service="api")
            # 迁移/运行角色分离：preview/production 下 api/worker 运行角色（app_runtime）不做 DDL，
            # 由 migrate 一次性服务（迁移角色/owner）负责建表、RLS 与函数；此处在启动期只做 RLS 就绪
            # 自检，未就绪即 fail-closed。仅本地开发（连接角色即 owner）才允许启动期迁移。
            if settings.database_migrate_on_startup:
                initialize_postgres(store)  # 业务表 + checkpoint 表 + RLS + 函数（幂等）
            require_postgres_ready(store)   # RLS 自检，未就绪 fail-closed
            if seed:
                seed_default(store)
            await pool.open()
            saver = create_checkpointer(pool)
            store._checkpointer = saver     # 供清理任务在 Worker 进程内取用（按需）
            app.state.checkpointer = saver
            app.state.chat_runner = ChatRunner(store, llm, saver, retriever=retriever,
                                               adapter=adapter, conf_threshold=conf_threshold)
            yield
            await pool.close()
    else:
        store = store or build_store(settings)
        llm = llm if llm is not None else build_llm(settings)
        checkpointer = checkpointer or InMemorySaver()
        retriever = retriever if retriever is not None else build_retriever(settings)
        runner = ChatRunner(store, llm, checkpointer, retriever=retriever,
                            adapter=adapter, conf_threshold=conf_threshold)

    app = FastAPI(title="电商售后多智能体工单系统", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
        # 统一领域错误响应；状态码与 message 由 DomainError 决定。
        return JSONResponse(status_code=exc.status_code,
                            content={"detail": exc.message, "code": exc.code, **exc.detail})

    app.state.settings = settings
    app.state.store = store
    app.state.launch_gate = launch_gate_report
    app.state.llm = llm
    app.state.checkpointer = checkpointer
    app.state.retriever = retriever
    app.state.adapter = adapter
    app.state.chat_runner = runner

    # CORS：生产优先统一反向代理 + 同源 /api，仅在明确跨域来源时开启白名单。
    if settings.enable_cors:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins_list or [],
            allow_credentials=settings.cors_allow_credentials,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(routes.router, prefix=settings.api_prefix)
    if seed and not postgres_mode:
        seed_default(store)
    return app


app = create_app()
