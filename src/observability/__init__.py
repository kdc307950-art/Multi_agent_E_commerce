"""自托管可观测模块（完全自托管纪律）。

构成：
- `logging.py`：全局 PII 脱敏日志总线。根因（红线 1.8 / 1.7）：订单地址、完整支付信息、
  凭据、卡片号**绝不进入明文日志**；同时 `tenant_id` / `session_id` / `operation_id`
  作为可追踪上下文附加在结构化日志中。本模块在 root logger 上安装全局脱敏过滤器，
  使**任何** logger（uvicorn / celery / 第三方）最终渲染的消息都经过脱敏。
- `tracing.py`：自托管 Langfuse trace 辅助。仅当 `langfuse_public_key` 配置时才启用；
  未配置或客户端异常时一律 no-op / 吞错，可观测绝不阻塞业务路径。trace 写入
  `tenant_id / session_id / environment` 到 metadata，输入输出经 PII 脱敏。
- `metrics.py`：依赖无关的 Prometheus 文本指标。**有界标签**（route/status/node），
  **绝不**把高基数 `tenant_id` 作为 Prometheus 标签；租户明细走受控审计查询。
"""

from src.observability.logging import (
    JsonFormatter,
    PIIRedactionFilter,
    configure_logging,
    get_logger,
    log_with_context,
)
from src.observability.metrics import MetricsRegistry, get_metrics
from src.observability.tracing import get_tracer, trace_span

__all__ = [
    "PIIRedactionFilter",
    "JsonFormatter",
    "configure_logging",
    "get_logger",
    "log_with_context",
    "MetricsRegistry",
    "get_metrics",
    "get_tracer",
    "trace_span",
]
