"""集中配置。

设计约束：完全自托管；LLM 使用本地/内网 OpenAI 兼容端点；存储可插拔（默认
SQLite/Memory 用于本地开发与测试，PostgreSQL 用于 Docker/预发布，其可用性待验证）。
未验证能力一律不宣称生产可用。

本文件同时承载：
- LLM 后端选择（mock | openai_compatible）与本地端点连接参数；
- 自托管检索组件（keyword 缺省 | milvus 可选）与 Milvus 连接参数；
- 敏感操作/意图的置信度阈值与能力矩阵相关的业务窗口配置；
- CrewAI/MCP 子智能体集成的门控开关。
"""
from __future__ import annotations

import os
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- 运行 ---
    env: str = Field(default="development")  # development / test / preview / production
    api_prefix: str = "/api"
    session_ttl_days: int = 7
    log_level: str = "INFO"
    # 演示/种子数据（TENANT-A、TENANT-B 及演示成员）是否允许自动创建。
    # 安全基线：仅 development/test 且本值为 true 才允许 seed_default；preview/production 一律禁止
    # （is_restricted_env 为 true 时无论本值如何都拒绝），演示租户/成员只用于本地开发/测试。
    # 首批真实租户与成员必须由受控迁移/运营脚本创建（见 scripts/create_bootstrapped_tenants.py），
    # 不通过 seed_default。默认 false（最不安全配置也被拒绝）。
    demo_seed_enabled: bool = False
    # 环境/配置版本号：随部署基线递增，写进发布记录（config_version 字段）用于审计配置漂移。
    deploy_config_version: str = "0.1.0"
    # 结构化 JSON 日志（带 tenant_id/session_id/operation_id 上下文，供日志平台采集）。
    # 默认关闭（开发用可读文本）；preview/production 建议开启并由自托管日志链路采集。
    log_json_format: bool = False

    # --- 认证（Mock 仅在 development/test 允许；preview/production 必须 real 并 fail-closed）---
    # auth_backend: mock | real。mock 只用于本地开发/单元测试；生产/预发布禁止启用 mock。
    auth_backend: str = "mock"
    # real 后端（HS256 JWT）所需配置；缺失或 env 为 preview/production 而仍走 mock 即 fail-closed。
    auth_jwt_secret: str = ""
    # JWT 固定声明：签发与校验使用同一组固定值，避免抽查不一致（iss/aud 不匹配一律拒绝）。
    # 签发始终写入 iss/aud；校验强制要求它们与配置一致（除非显式留空则跳过该项校验，仅限本地）。
    auth_jwt_issuer: str = "after-sales"
    auth_jwt_audience: str = "after-sales-web"
    # 固定令牌过期时间（秒）。签发时 exp = iat + 该值；不随登录态/客户端变化，确保可预期。
    auth_jwt_ttl_seconds: int = 3600
    # 当前签名密钥的 kid 标识；留空则自动由 `sha256(auth_jwt_secret)[:16]` 派生。JWT header 携带 kid。
    auth_jwt_kid: str = ""
    # 密钥轮换策略：**移入轮换池的旧密钥**（逗号分隔，多为上次轮换前的 `AUTH_JWT_SECRET`）。
    # 仅用于**校验**（容忍窗口内旧令牌仍可验证），绝不用于新签发；签发永远只用当前 `auth_jwt_secret`。
    # 轮换步骤：新密钥写入 AUTH_JWT_SECRET，上一个密钥追加到 AUTH_JWT_ROTATED_SECRETS，观察无新旧令牌
    # 校验失败后（超过 `auth_jwt_ttl_seconds`）从列表移除。未知 kid 的令牌一律拒绝（fail-closed）。
    auth_jwt_rotated_secrets: str = ""
    # 登录端点凭据表（JSON 字符串）：`{"<tenant_id>:<user_id>": "<PHC 哈希>"}`。
    # 哈希算法由 `auth_credential_hash` 决定（argon2id | bcrypt）。仅登录（签发 JWT）用；
    # 未配置对应条目或表为空时登录失败（fail-closed）并审计。凭据（明文与哈希）永不入日志。
    auth_login_credentials: str = ""
    # 登录凭据哈希算法：argon2id（默认，推荐）| bcrypt。禁止使用无盐 SHA-256（弱哈希）。
    # 服务端只保存 PHC 字符串（如 `$argon2id$v=19$m=65536,t=3,p=4$...`），绝不明文存库/入日志。
    # 生成哈希用 `python scripts/hash_login_credentials.py`（也可由 CI/密钥管理在部署时生成）。
    auth_credential_hash: str = "argon2id"
    # 登录限流与失败退避（/api/auth/login 专用；双维度：账号 + IP）。
    # 存储后端：memory（单进程/无状态，默认，用于开发与默认部署；多副本需 redis）| redis（分布式）。
    login_rate_limit_store: str = "memory"
    # 限流窗口（秒）。账号维度与 IP 维度共用同一时间窗。
    login_rate_limit_window_seconds: float = 300.0
    # 单账号在窗口内的最大登录尝试次数；超限拒绝并审计（login_rate_limited）。
    login_account_rate_limit: int = 5
    # 单 IP 在窗口内的最大登录尝试次数（跨账号聚合）；超限拒绝并审计（ip_rate_limited）。
    login_ip_rate_limit: int = 30
    # 失败退避：同一账号连续失败 `login_backoff_max_failures` 次后，锁定
    # `login_backoff_base_seconds * 2^(失败次数-阈值)`（封顶 login_backoff_max_seconds）。
    login_backoff_max_failures: int = 5
    login_backoff_base_seconds: float = 1.0
    login_backoff_max_seconds: float = 900.0
    # 受信任反向代理头的来源数（nginx 之后取最后一个可信 X-Forwarded-For 值作为客户端 IP）。
    # 生产/预览统一由自托管 nginx 终结 HTTPS 后转发，故 X-Forwarded-For 可信；直接暴露时设 0。
    login_trusted_proxy_depth: int = 1

    # --- 首次上线门控（少量租户 / 仅审批后执行 / 全量审计 / 人工复核）---
    # 逗号分隔的租户白名单。非空时仅这些租户可创建会话/聊天/审批操作（其它租户拒绝并审计）。
    # 首次上线应限定为少量租户；全量上线前清空（或把所有租户加入）即放开。
    launch_allowed_tenants: str = ""
    # 敏感写（退款/退货/改址）必须仅经唯一 human_approval 审批后执行；false 将触发门控告警。
    launch_require_approval: bool = True
    # 首次上线要求全量审计（每条敏感/拒绝路径留痕）；false 触发门控告警。
    launch_full_audit: bool = True
    # 首次上线要求人工复核（转人工/审批人留痕）；false 触发门控告警。
    launch_manual_review: bool = True
    # 严格门控：受限环境（preview/production）下 verify_launch_gate 对违规项直接抛错（fail-closed）。
    # 默认关闭（开发/测试宽松），上线时由 verify_launch_gate 脚本以 --strict 强制执行。
    launch_gate_strict: bool = False

    # --- CORS / 统一反向代理 ---
    # 生产优先"统一反向代理 + 同源 /api"（前端 NEXT_PUBLIC_API_BASE_URL 默认 /api）。
    # 仅在确实跨域且已明确来源时开启 CORS；来源白名单逗号分隔。
    enable_cors: bool = False
    cors_allow_origins: str = ""
    cors_allow_credentials: bool = False

    # --- 存储 ---
    # storage_backend: memory | sqlite | postgres。postgres 依赖 Docker/预发布环境，未验证。
    storage_backend: str = "memory"
    # sqlite 文件路径（storage_backend=sqlite 时使用）
    sqlite_path: str = "./data/app.db"
    # postgres 运行连接（storage_backend=postgres 时使用；需 Docker 提供 PostgreSQL）。
    # preview/production 使用**运行角色**连接：建议 `app_runtime`（LOGIN NOINHERIT NOSUPERUSER
    # NOBYPASSRLS，仅 GRANT 后的 DML），绝不使用表 owner/迁移角色。绝不内置演示密码——该值必须
    # 由服务器环境密钥注入；为空且 storage_backend=postgres 时连接即失败（fail-closed）。
    database_url: str = Field(default="", validate_default=True)
    # 数据库迁移角色连接（与运行角色分离）。仅由 migrate 一次性服务 / 启动期 DDL 使用，
    # 需要 CREATE/ALTER 权限；api/worker 运行角色绝不连接它。留空则回退到 database_url（仅限本地开发）。
    database_migrator_url: str = ""
    # 是否允许 api/worker 在启动期执行 DDL 迁移。本地开发可开（连接角色即 owner）；preview/production
    # 必须关闭（运行角色无 CREATE/ALTER），把 DDL 交给 migrate 一次性服务执行。
    database_migrate_on_startup: bool = True
    # 迁移阶段为 `app_runtime` 运行角色设置的口令（创建/更新角色用）。仅 migrate 服务需要；
    # 由服务器环境密钥注入，禁止硬编码。留空则创建角色时不设置口令（仅限本地开发）。
    app_runtime_password: str = ""
    # PostgresStore / checkpoint 连接池（业务数据面，SQLAlchemy/psycopg3 + AsyncConnectionPool）
    postgres_pool_size: int = 10
    postgres_max_overflow: int = 10
    # 会话/检查点清理批次与周期（滑动 7 天保留后的过期清理）
    cleanup_batch_size: int = 100
    session_cleanup_interval_seconds: int = 3600

    # --- LLM（本地/内网 OpenAI 兼容端点，完全自托管）---
    # 默认使用内置规则 Mock；指向真实端点时需配置 base_url/model/api_key。
    # 完全自托管红线：LLM 端点只能落在受控网络。llm_allowed_hosts 为空时：
    #   - 非受限环境（development/test）：默认仅放行 loopback 与 RFC1918 私网，禁止公网 SaaS；
    #   - 受限环境（preview/production）：强制要求显式白名单，否则 fail-closed（拒绝访问任何端点）。
    # 配置为逗号分隔的 host / IP / CIDR（如 "localhost,10.0.0.0/8,192.168.0.0/16"）。
    llm_backend: str = "mock"  # mock | openai_compatible
    llm_base_url: str = "http://localhost:8001/v1"
    llm_api_key: str = "sk-local"
    llm_model: str = "self-hosted-model"
    llm_timeout_seconds: float = 10.0   # 单次调用总期限（deadline，不叠加超时）
    llm_max_retries: int = 2            # 同类型错误有界重试
    # 端点网络白名单：逗号分隔的 host / IP / CIDR。受限环境为空即 fail-closed。
    llm_allowed_hosts: str = ""
    # 是否启用脱敏日志（掩码手机号/地址/订单号/密钥/授权头；默认开启）。
    llm_log_redact: bool = True

    # --- 模型能力矩阵（写操作白名单，评测驱动，不可硬编码为真实模型名）---
    # 只有"通过写操作专项评测"的明确 model id 才能写入本白名单；其余一律拒绝写并转人工。
    # 配置为逗号分隔的 model id。preview/production 要求显式白名单 + 提供评测报告，
    # 二者交集才生效；否则 fail-closed（无任何模型可写）。
    high_confidence_models: str = ""
    # 写操作专项评测报告路径（JSON）。preview/production 用于校验：白名单模型必须在该报告
    # 中标记 write_op_pass=true，否则视为未评测，拒绝写操作。
    llm_eval_report_path: str = ""

    # --- 意图 / 模型输出校验 ---
    # 低于此置信度的意图一律 fail-closed 转人工，不做自动路由/写操作。
    intent_confidence_threshold: float = 0.7
    # 打开即要求所有结构化模型输出（意图/幻觉检测等）经严格 schema 校验，非法即 fail-closed。
    strict_model_output: bool = True

    # --- 自托管检索组件 ---
    # retrieval_backend: keyword（缺省，确定性、无外部依赖）| milvus（可选，自托管 Milvus Lite）
    retrieval_backend: str = "keyword"
    retrieval_top_k: int = 4
    # Milvus 连接（retrieval_backend=milvus 时使用；Milvus Lite 为本地文件，完全自托管）
    milvus_uri: str = "./data/milvus_local.db"
    milvus_db_name: str = "after_sales"
    milvus_collection: str = "policy"
    milvus_embedding_dim: int = 128   # 自托管确定性 hash 向量维度（无外部 embedding 模型）

    # --- 业务规则 / 资格校验 ---
    # 退款/退货资格窗口（自签收/发货起的天数；超过则资格不明转人工）
    refund_eligibility_days: int = 7
    return_eligibility_days: int = 30
    # 审批超时：审批单超过该秒数仍未决策则标记 timeout 并把操作转人工（保留 operation_id）。
    approval_timeout_seconds: float = 3600.0
    # 金额校验：退款金额不得超过订单实付金额，且必须为正（非零）。0 表示启用"金额必须>0"。
    max_refund_amount_cap: float = 0.0   # 预留：如需全局金额上限可配置，0 = 不启用
    # 退款/退货/改址工具参数规格
    tool_require_strict_schema: bool = True

    # --- 资金/业务执行面（refund/return/address 的审批后执行）---
    # execution_mode: shadow（第一轮：只生成待执行记录 + 模拟回执，不触真实资金）
    #               | live（受控执行开关：调用 FundsProvider，需沙箱端到端验收后才允许开启）
    execution_mode: str = "shadow"
    # 外部资金/业务网关提供方。当前仅 "mock"（沙箱）；真实网关接入属后期替换点。
    execution_provider: str = "mock"
    # 回调 HMAC-SHA256 验签密钥（外部网关签名共享密钥；生产必须由环境密钥注入）。
    execution_callback_hmac_secret: str = "shadow-callback-secret"
    # 提交后等待最终回调确认的超时（秒）；超时未确认进入对账任务收口。
    execution_confirm_timeout_seconds: float = 900.0

    # --- CrewAI / MCP 子智能体集成（门控；默认关闭，走内置确定性节点）---
    # 关闭时业务节点使用内置（静态绑定）逻辑；打开时业务节点尝试委派给 CrewAI 子智能体。
    # 打开后必须提供真实调用链（本地模型端点）并通过集成测试；未验证不得在生产开启。
    crewai_enabled: bool = False
    crewai_model: str = ""        # 空则回退到 llm_model
    crewai_allow_delegation: bool = True

    # --- 队列 / 锁（Celery + Redis；本机未装 Redis 时跳过）---
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/0"

    # --- 可观测（自托管 Langfuse；未配置则不启用）---
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "http://localhost:3002"

    @property
    def is_test(self) -> bool:
        return self.env == "test"

    @property
    def is_restricted_env(self) -> bool:
        """preview/production 为受限环境：禁止 Mock 认证，必须 fail-closed。"""
        return self.env in {"preview", "production"}

    @property
    def cors_origins_list(self) -> list[str]:
        return [s.strip() for s in self.cors_allow_origins.split(",") if s.strip()]

    @property
    def effective_llm_model(self) -> str:
        """能力矩阵采用的模型名：CrewAI 若未单独指定则回退到 llm_model。"""
        return self.crewai_model or self.llm_model

    @property
    def llm_allowed_host_list(self) -> list[str]:
        """端点点网络白名单列表（去空白、去空项）。"""
        return [s.strip() for s in self.llm_allowed_hosts.split(",") if s.strip()]

    @property
    def high_confidence_model_set(self) -> set[str]:
        """写操作能力矩阵白名单（模型名显式白名单）。"""
        return {s.strip() for s in self.high_confidence_models.split(",") if s.strip()}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
