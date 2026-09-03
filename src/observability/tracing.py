"""自托管 Langfuse trace 辅助。

纪律：
- 完全自托管：仅当 `LANGFUSE_PUBLIC_KEY` 配置时才启用；未配置时 `trace_span` 是
  no-op（不产生任何网络/副作用），开发与测试走空实现。
- 不阻塞业务：可观测链路任何异常（导入失败、客户端异常、网络错误）一律吞掉并空跑，
  绝不让观测故障影响资金/对话主链路（红线：可观测是旁路，不是依赖）。
- 元数据可追踪：每次 trace 写入 `tenant_id / session_id / environment` 到 metadata；
  `user_id` 作为 Langfuse 用户维度（非 PII 的 id）。输入输出经 `redact()` 脱敏。
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterator

from src.config import get_settings
from src.llm.security import redact

try:  # 可选依赖：未安装 / 不可用时整体降级为 no-op
    from langfuse import Langfuse  # type: ignore
except Exception:  # pragma: no cover - 仅当 langfuse 未安装
    Langfuse = None  # type: ignore


def _redact_payload(value: Any) -> str | None:
    """把 trace 的输入/输出序列化为 JSON 并整体脱敏（绝不含地址/支付/密钥明文）。"""
    if value is None:
        return None
    try:
        return redact(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return redact(str(value))


class Tracer:
    """Langfuse 追踪器。`enabled=False` 时全链路 no-op。"""

    def __init__(self, *, public_key: str = "", secret_key: str = "",
                 host: str = "", environment: str = "") -> None:
        self._public_key = public_key
        self._secret_key = secret_key
        self._host = host
        self._environment = environment or "development"
        self._client: Any = None
        # 是否具备启用条件取决于密钥；客户端可注入（测试）,否则在解析层降级 no-op。
        self._enabled = bool(public_key and secret_key)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _ensure_client(self) -> Any:
        if self._client is None:
            if Langfuse is None:
                return None
            try:
                self._client = Langfuse(
                    public_key=self._public_key,
                    secret_key=self._secret_key,
                    host=self._host,
                )
            except Exception:
                self._enabled = False
                return None
        return self._client

    @contextmanager
    def span(self, name: str, *, tenant_id: str | None = None,
             session_id: str | None = None, user_id: str | None = None,
             input: Any = None, output: Any = None, **extra: Any) -> Iterator[None]:
        if not self._enabled:
            yield
            return
        client = self._ensure_client()
        if client is None:
            yield
            return
        span_obj = None
        created = False
        # 观测初始化失败（建 trace/span）不阻塞业务：no-op 直通。
        try:
            meta: dict[str, Any] = {"environment": self._environment}
            if tenant_id:
                meta["tenant_id"] = tenant_id
            if session_id:
                meta["session_id"] = session_id
            meta.update({k: (redact(v) if isinstance(v, str) else v)
                         for k, v in extra.items()})
            trace = client.trace(name=name, metadata=meta,
                                 session_id=session_id or None,
                                 user_id=user_id or None)
            span_obj = trace.span(name=name, input=_redact_payload(input))
            created = True
        except Exception:
            yield
            return
        try:
            yield
        finally:
            # 业务异常照常向上传播；仅收尾（end）错误被吞掉，不影响业务。
            if created and span_obj is not None:
                try:
                    # output 可为可调用对象：在退出时求值（流结束后记录动态结果）。
                    out = output() if callable(output) else output
                    span_obj.end(output=_redact_payload(out))
                except Exception:
                    pass


_NULL = Tracer()


def get_tracer(settings=None) -> Tracer:
    """返回全局追踪器。未配置 Langfuse 时返回 no-op。"""
    if settings is None:
        settings = get_settings()
    if not settings.langfuse_public_key:
        return _NULL
    return Tracer(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_host or "http://localhost:3002",
        environment=settings.env,
    )


@contextmanager
def trace_span(name: str, *, tenant_id: str | None = None,
               session_id: str | None = None, user_id: str | None = None,
               input: Any = None, output: Any = None, **extra: Any) -> Iterator[None]:
    """便捷追踪上下文：未配置 Langfuse 时为 no-op。"""
    with get_tracer().span(name, tenant_id=tenant_id, session_id=session_id,
                           user_id=user_id, input=input, output=output, **extra):
        yield
