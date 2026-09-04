"""依赖无关的 Prometheus 指标（自托管监控链路）。

纪律（红线 1.7）：Prometheus 指标**不得**使用高基数 `tenant_id` 作为标签——租户明细
走受控审计/聚合查询，不落到监控标签上，避免高基数标签爆炸。本模块在注册指标时
显式拒绝把 `tenant_id`（及其它高基数/敏感维度）用作标签（抛 `ValueError`）。

标签应使用**有界**维度（如 `route`、`status`、`node`、`kind`）。渲染为 Prometheus
文本格式，交由 `/api/metrics` 暴露；再被自托管 Prometheus 抓取、Grafana 展示。
"""
from __future__ import annotations

import threading
from typing import Any

# 禁止作为监控标签的键：租户是高基数，只能在受控审计查询里明细化。
_FORBIDDEN_LABELS = frozenset({
    "tenant_id", "user_id", "thread_id", "order_id", "operation_id",
    "client_request_id", "approval_id", "execution_id", "request_id",
})

# 执行/处理延迟直方图桶（秒）
DEFAULT_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0)


class MetricsRegistry:
    """线程安全、依赖无关的指标注册表（counter + histogram）。"""

    def __init__(self, buckets: tuple[float, ...] = DEFAULT_BUCKETS) -> None:
        self._lock = threading.Lock()
        self._buckets = buckets
        # name -> 已声明的标签名（用于校验/输出稳定）
        self._declared: dict[str, tuple[str, ...]] = {}
        # (name, labels_tuple) -> float
        self._counters: dict[tuple, float] = {}
        # (name, labels_tuple) -> (total, [c0..cn-1], sum)
        self._histograms: dict[tuple, tuple[int, list, float]] = {}
        # (name, labels_tuple) -> float（gauge：可由外部受控脚本写入的绝对值）
        self._gauges: dict[tuple, float] = {}

    @staticmethod
    def _check_labels(label_names: tuple[str, ...], labels: dict[str, Any]) -> None:
        forbidden = [k for k in label_names if k in _FORBIDDEN_LABELS]
        if forbidden:
            raise ValueError(
                f"指标标签不得包含高基数/敏感维度：{forbidden}（租户明细走受控审计查询）")
        for k in labels:
            if k not in label_names:
                raise ValueError(f"未知标签 {k}; 应属于 {label_names}")

    @staticmethod
    def _lkey(labels: dict[str, Any]) -> tuple[tuple[str, str], ...]:
        return tuple(sorted((str(k), str(v)) for k, v in labels.items()))

    # ---- Counter ----
    def increase(self, name: str, amount: float = 1.0,
                 label_names: tuple[str, ...] = (),
                 labels: dict[str, Any] | None = None) -> None:
        labels = labels or {}
        self._check_labels(label_names, labels)
        key = (name, self._lkey(labels))
        with self._lock:
            self._declared.setdefault(name, label_names)
            self._counters[key] = self._counters.get(key, 0.0) + amount

    def counter(self, name: str, label_names: tuple[str, ...] = (),
                labels: dict[str, Any] | None = None) -> None:
        self.increase(name, 1.0, label_names, labels)

    # ---- Histogram ----
    def observe(self, name: str, value: float,
                label_names: tuple[str, ...] = (),
                labels: dict[str, Any] | None = None) -> None:
        value = value if value >= 0 else 0.0
        labels = labels or {}
        self._check_labels(label_names, labels)
        key = (name, self._lkey(labels))
        with self._lock:
            self._declared.setdefault(name, label_names)
            total, buckets, sm = self._histograms.get(key, (0, [0] * len(self._buckets), 0.0))
            buckets = list(buckets)
            for i, upper in enumerate(self._buckets):
                if value <= upper:
                    buckets[i] += 1
            self._histograms[key] = (total + 1, buckets, sm + value)

    # ---- Gauge ----
    def set(self, name: str, value: float,
            label_names: tuple[str, ...] = (),
            labels: dict[str, Any] | None = None) -> None:
        """设置一个 gauge 绝对值（如对账最后运行时间 / 备份恢复 RPO/RTO 实测）。

        仅供自托管受控脚本/任务写入**有界**维度（如 component=pg_backup），
        绝不接受 tenant_id/user_id 等高基数/敏感标签（由 `_check_labels` 拒绝）。
        """
        labels = labels or {}
        self._check_labels(label_names, labels)
        key = (name, self._lkey(labels))
        with self._lock:
            self._declared.setdefault(name, label_names)
            self._gauges[key] = float(value)

    # ---- 渲染 ----
    def render(self) -> str:
        lines: list[str] = []
        seen_families: set[str] = set()

        def fmt_labels(label_names: tuple[str, ...], labels: tuple[tuple[str, str], ...],
                       extra: str = "") -> str:
            parts = [f'{k}="{v}"' for k, v in labels]
            if extra:
                parts.append(extra)
            return "{" + ",".join(parts) + "}" if parts else ""

        def emit_type(name: str, kind: str) -> None:
            # 每个指标族只输出一次 `# TYPE` 头（否则同一指标名的多个系列会出现重复 TYPE 行，
            # 让 Prometheus 文本抓取报错）。
            if name not in seen_families:
                lines.append(f"# TYPE {name} {kind}")
                seen_families.add(name)

        with self._lock:
            counters = list(self._counters.items())
            declared = dict(self._declared)
            hists = list(self._histograms.items())
            gauges = list(self._gauges.items())

        for (name, labels), value in counters:
            emit_type(name, "counter")
            lines.append(f"{name}{fmt_labels(declared.get(name, ()), labels)} {value:g}")

        for (name, labels), value in gauges:
            emit_type(name, "gauge")
            lines.append(f"{name}{fmt_labels(declared.get(name, ()), labels)} {value:g}")

        for (name, labels), (total, buckets, sm) in hists:
            emit_type(name, "histogram")
            for i, upper in enumerate(self._buckets):
                lines.append(
                    f"{name}_bucket{fmt_labels(declared.get(name, ()), labels, 'le=' + repr(upper))} {buckets[i]:g}")
            lines.append(
                f"{name}_bucket{fmt_labels(declared.get(name, ()), labels, 'le=+Inf')} {total:g}")
            lines.append(f"{name}_sum{fmt_labels(declared.get(name, ()), labels)} {sm:g}")
            lines.append(f"{name}_count{fmt_labels(declared.get(name, ()), labels)} {total:g}")

        return "\n".join(lines) + ("\n" if lines else "")

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._histograms.clear()
            self._gauges.clear()
            self._declared.clear()


_registry = MetricsRegistry()


def get_metrics() -> MetricsRegistry:
    return _registry
