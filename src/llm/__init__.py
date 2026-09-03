"""LLM 包：Mock / 本地 OpenAI 兼容端点选择。

完全自托管约束：不引入任何公有 SaaS LLM。LLM_BACKEND 的取值决定实际实例：
- mock：内置确定性规则实现（本地开发/单元测试）；
- openai_compatible：调用本地/内网 OpenAI 兼容端点（vLLM/Ollama 等）。

build_llm 依据配置返回对应实现；默认（mock）不与任何模型端点通信。能力矩阵（写操作白名单）
由 src.llm.capability 评测驱动解析，构造实例时注入，节点层门控据此 fail-closed 转人工。
"""
from __future__ import annotations

import os

from src.llm.base import BaseLLM, LLMError, LLMOutputError, LLMUnavailableError
from src.llm.capability import DEFAULT_DEV_WRITE_MODELS, resolve_high_confidence_models
from src.llm.mock import MockLLM
from src.llm.openai_compatible import OpenAICompatibleLLM


def build_llm(settings) -> BaseLLM:
    """按 settings.llm_backend 构造 LLM 实例。

    - mock：MockLLM(model=settings.llm_model, high_confidence_models=<评测驱动白名单>)
    - openai_compatible：OpenAICompatibleLLM 指向自托管端点，并注入网络白名单/脱敏/
      评测驱动白名单。
    未知后端抛 ValueError（配置错误，快速失败）。
    """
    backend = (settings.llm_backend or "mock").strip().lower()
    whitelist = resolve_high_confidence_models(settings)
    allowed_hosts = [h.strip() for h in (getattr(settings, "llm_allowed_host_list", []) or [])]
    restricted = bool(getattr(settings, "is_restricted_env", False))
    if backend == "mock":
        return MockLLM(model=settings.llm_model, high_confidence_models=whitelist)
    if backend == "openai_compatible":
        return OpenAICompatibleLLM(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
            strict=settings.strict_model_output,
            allowed_hosts=allowed_hosts,
            restricted=restricted,
            redact_log=bool(getattr(settings, "llm_log_redact", True)),
            high_confidence_models=whitelist,
        )
    raise ValueError(f"未知 LLM 后端: {backend!r}（可选 mock | openai_compatible）")


__all__ = [
    "BaseLLM",
    "LLMError",
    "LLMOutputError",
    "LLMUnavailableError",
    "DEFAULT_DEV_WRITE_MODELS",
    "MockLLM",
    "OpenAICompatibleLLM",
    "build_llm",
]
