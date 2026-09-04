"""LLM 调用熔断器（circuit breaker）。

完全自托管纪律（Agent 宪法 1.8 / 《错误处理与回退机制》§3.2）：端点故障必须在**有界时间**内
被隔离，避免在端点持续不可用时反复打满超时/重试；熔断打开后**快速失败**（fail-closed），
把调用转人工（`LLMUnavailableError` 在节点层 fail-closed），绝不以低档模型降级执行。

状态机：
- CLOSED：正常放行。连续失败达到 `failure_threshold` → OPEN。
- OPEN：快速拒绝（`allow()` 返回 False），直到 `cooldown_seconds` 过去 → HALF_OPEN（放行一次探针）。
- HALF_OPEN：放行一次；成功 → CLOSED；失败 → 重新 OPEN（重置计时）。

`failure_threshold <= 0` 表示禁用熔断（默认不放宽生产语义；仅测试/本地需要时可通过配置关闭）。
线程安全（并发请求共享同一 instance）。
"""
from __future__ import annotations

import threading
import time
from enum import Enum


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """按连续失败阈值 + 冷却时间熔断，打开期间快速失败。"""

    def __init__(self, *, failure_threshold: int = 5, cooldown_seconds: float = 30.0,
                 now_fn=None) -> None:
        self._threshold = failure_threshold
        self._cooldown = cooldown_seconds
        self._now = now_fn or time.time
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = 0.0
        # HALF_OPEN 仅允许一个并发探针，其他请求继续快速失败。
        self._probe_in_flight = False
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._threshold > 0

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def allow(self) -> bool:
        """是否放行本次调用。打开期间快速失败（返回 False）。"""
        if not self.enabled:
            return True
        with self._lock:
            if self._state is CircuitState.OPEN:
                if self._now() - self._opened_at >= self._cooldown:
                    # 冷却结束 → 半开，放行一次探针。
                    self._state = CircuitState.HALF_OPEN
                    self._probe_in_flight = True
                    return True
                return False
            if self._state is CircuitState.HALF_OPEN:
                # 半开探针尚未收口时，拒绝并发请求，避免探针风暴。
                if self._probe_in_flight:
                    return False
                self._probe_in_flight = True
                return True
            # CLOSED：正常放行。
            return True

    def on_success(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._consecutive_failures = 0
            self._state = CircuitState.CLOSED
            self._probe_in_flight = False

    def on_failure(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._consecutive_failures += 1
            if self._state is CircuitState.HALF_OPEN:
                # 半开探针失败 → 重新 OPEN。
                self._state = CircuitState.OPEN
                self._opened_at = self._now()
                self._probe_in_flight = False
                return
            if self._consecutive_failures >= self._threshold:
                self._state = CircuitState.OPEN
                self._opened_at = self._now()

    def __repr__(self) -> str:
        return f"<CircuitBreaker state={self._state.value} failures={self._consecutive_failures}>"
