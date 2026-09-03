"""项目级日志总线：全局 PII 脱敏 + 租户可追踪 + 可选 JSON 结构化。

红线（Agent 宪法 1.8 / 1.7）：订单地址、完整支付信息、凭据、卡片号**绝不进入明文日志**；
同时 tenant_id / session_id / operation_id 必须作为可追踪上下文保留在结构化日志中。

实现策略：
- `PIIRedactionFilter` 安装在 root logger（及其 handler）上。由于 Python logging 的
  `Handler.handle()` 先执行过滤器、再执行 formatter，因此**任何** logger（uvicorn / celery /
  第三方）在最终渲染消息前都会经过 `redact()` 脱敏，从源头阻断地址/支付/密钥落盘。
- 结构化日志（JsonFormatter）把 `tenant_id/session_id/operation_id` 等上下文写入 `extra`
  并以 JSON 输出；脱敏过滤器同样作用于这些字段（敏感键名一律掩码，标识符键名原样保留）。
- `configure_logging()` 在 API / worker 启动时调用一次，统一接管 root logger。
"""
from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any

from src.llm.security import redact

# 标识符/上下文键：原样保留（它们是引用，不是 PII）。
_CONTEXT_KEYS = frozenset({
    "tenant_id", "session_id", "thread_id", "operation_id", "request_id",
    "trace_id", "run_id", "approval_id", "execution_id", "stream_id",
    "channel", "service", "environment", "node",
})
# 敏感键名：命中即掩码其值（地址/支付/卡号/手机号/密钥）。
_PII_EXTRA_KEY = re.compile(
    r"(address|addr|payment|card|phone|mobile|credential|secret|token|"
    r"api_?key|password|id_card|bank|account|idnum|credit)",
    re.IGNORECASE,
)

__all__ = [
    "PIIRedactionFilter",
    "JsonFormatter",
    "configure_logging",
    "get_logger",
    "log_with_context",
    "redact",
]


def _redact_value(value: Any) -> Any:
    """对结构化字段做深度脱敏：字符串按敏感键掩码，容器递归处理。"""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: (_redact_value(v) if _PII_EXTRA_KEY.search(str(k)) else v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(v) for v in value]
    return value


class PIIRedactionFilter(logging.Filter):
    """全局脱敏过滤器。

    任何 logger 在最终 emit 前都会执行：把渲染后的消息用 `redact()` 掩码，并掩码
    `extra` 中命中敏感键名的字段；标识符上下文键原样保留，保证可追踪。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
            record.msg = redact(rendered)
            record.args = ()
            d = record.__dict__
            for key in list(d.keys()):
                if key in _CONTEXT_KEYS:
                    continue
                val = d[key]
                if isinstance(val, str) and _PII_EXTRA_KEY.search(key):
                    d[key] = redact(val)
                elif isinstance(val, (dict, list)):
                    d[key] = _redact_value(val)
        except (ValueError, TypeError):
            # 脱敏失败绝不阻断日志输出（宁可多打也不丢）。
            pass
        return True


class JsonFormatter(logging.Formatter):
    """JSON 结构化 formatter：输出时间戳/级别/模块/消息 + 上下文键。"""

    def __init__(self, service: str = "api", datefmt: str | None = None) -> None:
        super().__init__(datefmt=datefmt)
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "service": self.service,
            "message": record.getMessage(),
        }
        for key in _CONTEXT_KEYS:
            val = record.__dict__.get(key)
            if val is not None:
                payload[key] = val
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _ensure_redaction() -> None:
    """把脱敏过滤器同步到 root logger（幂等），确保未显式配置的 logger 也被覆盖。"""
    root = logging.getLogger()
    installed = any(isinstance(f, PIIRedactionFilter) for f in root.filters)
    if not installed:
        root.addFilter(PIIRedactionFilter())


def configure_logging(level: str = "INFO", json_format: bool = False,
                      service: str = "api") -> None:
    """统一接管 root logger：级别 + formatter + 全局脱敏过滤器。

    在 API / worker 启动时各调用一次。所有第三方 logger（uvicorn/celery 等）设为
    propagate 到 root，从而继承同一套脱敏与格式化。
    """
    _ensure_redaction()
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)
    stream = logging.StreamHandler(sys.stdout)
    if json_format:
        stream.setFormatter(JsonFormatter(service=service))
    else:
        stream.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    stream.addFilter(PIIRedactionFilter())
    root.addHandler(stream)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access",
                 "celery", "celery.task", "celery.worker"):
        logger = logging.getLogger(name)
        logger.setLevel(level.upper())
        logger.propagate = True
        logger.handlers.clear()
        logger.filters.clear()


def get_logger(name: str) -> logging.Logger:
    """返回项目 logger。全局 PII 脱敏过滤器已生效，消息自动脱敏。"""
    _ensure_redaction()
    return logging.getLogger(name)


def log_with_context(logger: logging.Logger, level: int, msg: str, *,
                     tenant_id: str | None = None,
                     session_id: str | None = None,
                     operation_id: str | None = None,
                     **fields: Any) -> None:
    """带上下文的日志：把 tenant/session/operation 及额外字段写入 extra 供结构化检索。

    敏感字段值在 emit 时由 `PIIRedactionFilter` 自动掩码；这里只负责把上下文挂到 extra。
    """
    extra: dict[str, Any] = {}
    if tenant_id is not None:
        extra["tenant_id"] = tenant_id
    if session_id is not None:
        extra["session_id"] = session_id
    if operation_id is not None:
        extra["operation_id"] = operation_id
    extra.update(fields)
    logger.log(level, msg, extra=extra)
