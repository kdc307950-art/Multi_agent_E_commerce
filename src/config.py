"""集中配置。

设计约束：完全自托管；LLM 使用本地/内网 OpenAI 兼容端点；存储可插拔（默认
SQLite/Memory 用于本地开发与测试，PostgreSQL 用于 Docker/预发布，其可用性待验证）。
未验证能力一律不宣称生产可用。
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

    # --- 存储 ---
    # storage_backend: memory | sqlite | postgres。postgres 依赖 Docker/预发布环境，未验证。
    storage_backend: str = "memory"
    # sqlite 文件路径（storage_backend=sqlite 时使用）
    sqlite_path: str = "./data/app.db"
    # postgres 连接（storage_backend=postgres 时使用；需 Docker 提供 PostgreSQL）
    database_url: str = Field(
        default="postgresql://user:pass@localhost:5432/langgraph", validate_default=True
    )

    # --- LLM（本地/内网 OpenAI 兼容端点，完全自托管）---
    # 默认使用内置规则 Mock；指向真实端点时需配置 base_url/model/api_key。
    llm_backend: str = "mock"  # mock | openai_compatible
    llm_base_url: str = "http://localhost:8001/v1"
    llm_api_key: str = "sk-local"
    llm_model: str = "self-hosted-model"

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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
