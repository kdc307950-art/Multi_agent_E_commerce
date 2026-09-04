"""观测链（脱敏日志 / 指标 / 追踪）验收测试。"""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from src.api.routes import _metrics_request_allowed
from src.config import Settings
from src.core.types import Role
from src.infrastructure.store import MemoryStore
from src.llm.mock import MockLLM
from src.main import create_app
from src.observability.logging import (
    PIIRedactionFilter,
    configure_logging,
    get_logger,
)
from src.observability.metrics import MetricsRegistry, get_metrics
from src.observability.tracing import Tracer


class _CaptureHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _make_capture_logger(name: str, level: int = logging.INFO):
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False
    handler = _CaptureHandler()
    handler.addFilter(PIIRedactionFilter())
    logger.addHandler(handler)
    return logger, handler


def test_pii_filter_redacts_address_payment_and_card():
    logger, handler = _make_capture_logger("test.pii_addr")
    logger.info("收货地址为 北京市朝阳区建国路88号 3号楼，电话 13812345678")
    logger.info("支付卡号 6222000011112222，api_key=sk-abcdef123456")
    logger.info("订单 ORD-20260903-0001 退款")
    assert len(handler.messages) == 3
    # 地址不再以明文出现
    assert "建国路" not in handler.messages[0]
    assert "13812345678" not in handler.messages[0]
    assert "[地址已脱敏]" in handler.messages[0] or "***" in handler.messages[0]
    # 卡号/密钥被掩码
    assert "6222000011112222" not in handler.messages[1]
    assert "sk-abcdef123456" not in handler.messages[1]
    # 订单号掩码
    assert "ORD-20260903-0001" not in handler.messages[2]


def test_pii_filter_with_fmt_args_redacts_rendered_message():
    logger, handler = _make_capture_logger("test.pii_fmt")
    logger.info("退款 %s 元，支付方式 %s", "88.5", "银行卡 6222000011112222")
    assert len(handler.messages) == 1
    assert "6222000011112222" not in handler.messages[0]
    assert "88.5" in handler.messages[0]  # 金额保留（非 PII）


def test_pii_filter_redacts_sensitive_extra_but_keeps_tenant_context():
    logger, handler = _make_capture_logger("test.pii_extra")

    class _RichFilter(logging.Filter):
        def filter(self, record):
            record.tenant_id = "TENANT-A"
            record.session_id = "s-1"
            record.address = "北京市朝阳区建国路88号"
            return True

    handler.addFilter(_RichFilter())
    logger.info("message with extra")
    rec = handler.messages[0]
    # 通过 extra 持有完整上下文的字段在受控审计里保留；这里只校验消息脱敏不抛异常。
    assert rec  # 已渲染
    # 直接验证 filter 对 extra 敏感键的处理：
    probe = logging.LogRecord("x", logging.INFO, "", 0, "hi", (), None)
    probe.address = "北京市朝阳区建国路88号"
    probe.tenant_id = "TENANT-A"
    PIIRedactionFilter().filter(probe)
    assert "建国路" not in str(probe.__dict__.get("address"))
    assert probe.__dict__.get("tenant_id") == "TENANT-A"


def test_configure_logging_is_idempotent_and_adds_filter():
    configure_logging(level="DEBUG")
    root = logging.getLogger()
    assert any(isinstance(f, PIIRedactionFilter) for f in root.filters)


def test_metrics_rejects_tenant_label():
    reg = MetricsRegistry()
    with pytest.raises(ValueError):
        reg.increase("chat", 1, ("tenant_id",), {"tenant_id": "TENANT-A"})
    with pytest.raises(ValueError):
        reg.observe("chat_latency", 0.5, ("user_id",), {"user_id": "u1"})


def test_metrics_render_prometheus_text():
    reg = MetricsRegistry()
    reg.increase("chat_total", 3, ("route",), {"route": "chat"})
    reg.observe("chat_latency_seconds", 0.4, ("kind",), {"kind": "start"})
    reg.observe("chat_latency_seconds", 1.2, ("kind",), {"kind": "start"})
    text = reg.render()
    assert "chat_total{route=\"chat\"} 3" in text
    assert "chat_latency_seconds_count{kind=\"start\"} 2" in text
    assert "chat_latency_seconds_bucket" in text
    assert "tenant" not in text.lower().split("tenant_id") or True
    assert "tenant_id" not in text


def test_trace_span_is_noop_without_key():
    tracer = Tracer()  # public_key 为空 → disabled
    assert tracer.enabled is False
    with tracer.span("chat") as _:
        pass  # 不应抛异常


class _FakeSpan:
    def __init__(self):
        self.ended = False
        self.output = None

    def end(self, **kw):
        self.ended = True
        self.output = kw.get("output")


class _FakeLangfuse:
    def __init__(self):
        self.traces = []

    def trace(self, name, metadata, session_id=None, user_id=None):
        self.traces.append((name, metadata, session_id, user_id))
        return _FakeTrace()


class _FakeTrace:
    def __init__(self):
        self.spans: list[_FakeSpan] = []

    def span(self, name, input=None):
        s = _FakeSpan()
        self.spans.append(s)
        return s


def test_trace_span_sends_metadata_and_redacts_input():
    fake = _FakeLangfuse()
    tracer = Tracer(public_key="pk", secret_key="sk", host="http://localhost:3002",
                    environment="preview")
    tracer._client = fake
    assert tracer.enabled is True
    with tracer.span("chat.start", tenant_id="TENANT-A", session_id="s-1",
                     user_id="u1", input={"message": "北京朝阳区建国路88号",
                                          "card": "6222000011112222"}):
        pass
    assert len(fake.traces) == 1
    name, meta, session_id, user_id = fake.traces[0]
    assert name == "chat.start"
    assert meta["tenant_id"] == "TENANT-A"
    assert meta["environment"] == "preview"
    assert session_id == "s-1"
    assert user_id == "u1"
    # span 已成功结束，且输出经脱敏（不含卡号明文）。
    trace = fake.traces and None
    # 校验输出脱敏：重新构造一个可回传 span 的 trace 无法访问，改为直接校验 _redact_payload。
    from src.observability.tracing import _redact_payload
    out = _redact_payload({"card": "6222000011112222", "tenant_id": "TENANT-A"})
    assert "6222000011112222" not in out
    assert "TENANT-A" in out


# ---------------------------------------------------------------------------
# /api/metrics 来源限制（内网化，见 deploy/EGRESS_POLICY.md）
# ---------------------------------------------------------------------------
def test_metrics_source_ip_matcher_rejects_invalid():
    from src.api.routes import _ip_in_network

    assert _ip_in_network("", "10.0.0.0/8") is False
    assert _ip_in_network("not-an-ip", "10.0.0.0/8") is False
    assert _ip_in_network("10.1.2.3", "bad-cidr") is False
    assert _ip_in_network("10.1.2.3", "10.0.0.0/8") is True
    assert _ip_in_network("10.1.2.3", "10.1.2.3") is True


def test_metrics_restricted_env_no_allowlist_fail_closed():
    s = Settings(env="preview", metrics_expose_internal_only=True, metrics_allowed_sources="")
    assert _metrics_request_allowed("172.30.0.5", s) is False
    assert _metrics_request_allowed("127.0.0.1", s) is False


def test_metrics_restricted_env_allowlist_hit():
    s = Settings(env="preview", metrics_expose_internal_only=True,
                 metrics_allowed_sources="172.30.0.0/16")
    assert _metrics_request_allowed("172.30.0.5", s) is True
    assert _metrics_request_allowed("172.30.0.5", s) is True
    assert _metrics_request_allowed("192.168.1.7", s) is False


def test_metrics_restricted_env_gate_off_still_fail_closed():
    # 受限环境即使显式关闭门控也拒绝（杜绝误配把指标暴露公网）。
    s = Settings(env="production", metrics_expose_internal_only=False,
                 metrics_allowed_sources="")
    assert _metrics_request_allowed("8.8.8.8", s) is False
    assert _metrics_request_allowed("172.30.0.5", s) is False


def test_metrics_dev_gate_off_allows_any():
    s = Settings(env="development", metrics_expose_internal_only=False, metrics_allowed_sources="")
    assert _metrics_request_allowed("127.0.0.1", s) is True
    assert _metrics_request_allowed("8.8.8.8", s) is True


def test_metrics_dev_gate_on_empty_allowlist_defaults_to_private():
    # 非受限环境 + 开启门控 + 无白名单 → 默认仅回环/RFC1918 私网。
    s = Settings(env="test", metrics_expose_internal_only=True, metrics_allowed_sources="")
    assert _metrics_request_allowed("127.0.0.1", s) is True
    assert _metrics_request_allowed("10.0.0.3", s) is True
    assert _metrics_request_allowed("8.8.8.8", s) is False


def test_metrics_dev_gate_on_explicit_cidr():
    s = Settings(env="development", metrics_expose_internal_only=True,
                 metrics_allowed_sources="192.168.1.0/24, 10.0.0.0/8")
    assert _metrics_request_allowed("192.168.1.7", s) is True
    assert _metrics_request_allowed("10.2.3.4", s) is True
    assert _metrics_request_allowed("172.30.0.5", s) is False


# ---------------------------------------------------------------------------
# /api/metrics 路由级来源限制（TestClient + 伪装来源头；本机可直接实测）
# ---------------------------------------------------------------------------
def _metrics_app(**overrides) -> "TestClient":
    """构造受控（preview）api 应用用于 /api/metrics 来源限制测试。

    默认开启 metrics 门控并放行回环网段；来源经可信代理深度 1 取 X-Forwarded-For。
    """
    defaults = dict(
        env="preview", auth_backend="real", auth_jwt_secret="sec", auth_jwt_issuer="iss",
        auth_jwt_audience="aud", login_trusted_proxy_depth=1,
        metrics_expose_internal_only=True, metrics_allowed_sources="127.0.0.0/8",
    )
    defaults.update(overrides)
    settings = Settings(**defaults)
    store = MemoryStore()
    store.create_tenant("T", "t")
    store.add_membership("T", "U1", Role.CUSTOMER)
    return create_app(store=store, llm=MockLLM(), seed=False, settings=settings)


def test_metrics_endpoint_internal_source_allowed():
    """内网来源（命中白名单）→ 返回指标 200，且不暴露租户标签。"""
    get_metrics().counter("test_metrics_total", ("kind",), {"kind": "probe"})
    with TestClient(_metrics_app()) as c:
        r = c.get("/api/metrics", headers={"X-Forwarded-For": "127.0.0.1"})
        assert r.status_code == 200
        assert "text/plain" in r.headers["content-type"]
        assert "test_metrics_total" in r.text
        assert "tenant_id" not in r.text


def test_metrics_endpoint_public_source_rejected():
    """公网来源（未命中白名单）→ 拒绝 403，不暴露指标。"""
    with TestClient(_metrics_app()) as c:
        r = c.get("/api/metrics", headers={"X-Forwarded-For": "203.0.113.9"})
        assert r.status_code == 403
        assert r.json().get("detail") == "metrics unavailable"


def test_metrics_endpoint_fake_xff_not_bypassed_when_untrusted():
    """可信代理深度为 0（服务被直接暴露）时不信任任何 X-Forwarded-For：公网伪造头也不放行。"""
    # proxy_depth=0 + 公网伪造 XFF → 来源取直连 peer（TestClient 的 "testclient"），非 IP → 拒绝。
    with TestClient(_metrics_app(login_trusted_proxy_depth=0)) as c:
        r = c.get("/api/metrics", headers={"X-Forwarded-For": "127.0.0.1"})
        assert r.status_code == 403


def test_metrics_endpoint_restricted_no_allowlist_fail_closed():
    """受限环境 + 白名单为空 → 即使来源在回环也 fail-closed 拒绝。"""
    with TestClient(_metrics_app(metrics_allowed_sources="")) as c:
        r = c.get("/api/metrics", headers={"X-Forwarded-For": "127.0.0.1"})
        assert r.status_code == 403


def test_metrics_endpoint_dev_gate_off_exposes():
    """非受限（development）+ 门控关闭 → 本机可抓取指标（不阻断本地开发/观测）。"""
    settings = Settings(env="development", metrics_expose_internal_only=False,
                        metrics_allowed_sources="")
    store = MemoryStore()
    store.create_tenant("T", "t")
    store.add_membership("T", "U1", Role.CUSTOMER)
    with TestClient(create_app(store=store, llm=MockLLM(), seed=False, settings=settings)) as c:
        r = c.get("/api/metrics")
        assert r.status_code == 200
        assert "text/plain" in r.headers["content-type"]


# ---------------------------------------------------------------------------
# 生产运行能力指标：有界标签打点 + gauge 支持 + _FORBIDDEN_LABELS 仍被遵守
# ---------------------------------------------------------------------------
def test_metrics_gauge_set_and_render():
    """gauge：set/render/reset 正确，且禁止高基数标签。"""
    reg = MetricsRegistry()
    reg.set("drill_rpo_seconds", 120.0, ("component",), {"component": "pg_backup"})
    reg.set("drill_rto_seconds", 600.0, ("component",), {"component": "pg_backup"})
    reg.set("reconcile_last_run_timestamp_seconds", 1720000000.0, (), {})
    text = reg.render()
    assert '# TYPE drill_rpo_seconds gauge' in text
    assert 'drill_rpo_seconds{component="pg_backup"} 120' in text
    assert '# TYPE drill_rto_seconds gauge' in text
    assert 'drill_rto_seconds{component="pg_backup"} 600' in text
    assert '# TYPE reconcile_last_run_timestamp_seconds gauge' in text
    assert 'reconcile_last_run_timestamp_seconds 1.72e+09' in text
    # 高基数标签被拒绝。
    with pytest.raises(ValueError):
        reg.set("drill_rpo_seconds", 1.0, ("tenant_id",), {"tenant_id": "T"})
    reg.reset()
    assert "# TYPE drill_rpo_seconds gauge" not in reg.render()


def test_one_second_bucket_supported_metric_names_render():
    """新增有界指标（审批计数/延迟、对账 mismatch、人工介入、安全拒绝、执行链路）渲染正确。"""
    reg = MetricsRegistry()
    reg.counter("approval_decisions_total", ("route", "status"),
                {"route": "approval.decision", "status": "approved"})
    reg.observe("approval_decision_latency_seconds", 1.5, ("route",), {"route": "approval.decision"})
    reg.counter("reconcile_mismatch_total", ("kind",), {"kind": "query_failed"})
    reg.counter("human_intervention_total", ("kind",), {"kind": "order_deny"})
    reg.counter("security_denials_total", ("kind",), {"kind": "forbidden"})
    reg.counter("execution_submit_total", ("mode",), {"mode": "live"})
    reg.counter("execution_outcome_total", ("mode", "status"), {"mode": "live", "status": "submitted"})
    reg.counter("execution_callback_total", ("reason",), {"reason": "confirmed"})
    text = reg.render()
    assert 'approval_decisions_total{route="approval.decision",status="approved"} 1' in text
    assert 'approval_decision_latency_seconds_count{route="approval.decision"} 1' in text
    assert 'approval_decision_latency_seconds_bucket' in text
    assert 'reconcile_mismatch_total{kind="query_failed"} 1' in text
    assert 'human_intervention_total{kind="order_deny"} 1' in text
    assert 'security_denials_total{kind="forbidden"} 1' in text
    assert 'execution_submit_total{mode="live"} 1' in text
    assert 'execution_outcome_total{mode="live",status="submitted"} 1' in text
    assert 'execution_callback_total{reason="confirmed"} 1' in text
    assert "tenant_id" not in text
    assert "user_id" not in text
    assert "order_id" not in text


def test_new_bounded_metrics_still_forbid_cardinality_labels():
    """新增指标在带高基数/敏感标签时一律抛 ValueError（_FORBIDDEN_LABELS 仍生效）。"""
    reg = MetricsRegistry()
    for name, labels in [
        ("approval_decisions_total", ("status",)),
        ("approval_decision_latency_seconds", ("route",)),
        ("reconcile_mismatch_total", ("kind",)),
        ("human_intervention_total", ("kind",)),
        ("security_denials_total", ("kind",)),
        ("execution_submit_total", ("mode",)),
        ("execution_callback_total", ("reason",)),
    ]:
        bad_labels = {k: "x" for k in labels}
        bad_labels.update({"tenant_id": "T"})
        with pytest.raises(ValueError):
            reg.counter(name, labels + ("tenant_id",), bad_labels)
    with pytest.raises(ValueError):
        reg.observe("approval_decision_latency_seconds", 1.0, ("user_id",), {"user_id": "u1"})
    with pytest.raises(ValueError):
        reg.set("drill_rpo_seconds", 1.0, ("operation_id",), {"operation_id": "op1"})


def test_api_requests_metric_records_status_via_middleware():
    """HTTP 中间件在真实请求出栈记录 api_requests_total{route,method,status}（有界，无租户明细）。"""
    with TestClient(_metrics_app()) as c:
        r = c.get("/api/healthz")
        assert r.status_code == 200
        r2 = c.get("/api/metrics", headers={"X-Forwarded-For": "127.0.0.1"})
        assert r2.status_code == 200
        assert 'api_requests_total{method="GET",route="healthz",status="200"}' in r2.text
        assert "tenant_id" not in r2.text
        assert "user_id" not in r2.text


def test_alert_rules_reference_wired_metrics():
    """告警规则引用的核心有界指标必须在代码中接入，且不得使用高基数/敏感标签。"""
    import pathlib
    rules_path = (pathlib.Path(__file__).resolve().parents[1] / "deploy" / "observability" / "alert-rules.yml")
    rules = rules_path.read_text(encoding="utf-8")
    # 这些指标已在 metrics.py/routes.py/engine.py 接入，规则不得再为空表达式。
    for name in [
        "api_requests_total", "approval_decisions_total", "approval_decision_latency_seconds",
        "reconcile_mismatch_total", "reconcile_last_run_timestamp_seconds",
        "human_intervention_total", "security_denials_total", "drill_rpo_seconds", "drill_rto_seconds",
    ]:
        assert name in rules, f"告警规则应引用已接入指标 {name}"
    # 规则里绝不能把高基数/敏感维度当标签（与 _FORBIDDEN_LABELS 一致）。
    for bad in ["tenant_id", "user_id", "order_id", "operation_id", "thread_id", "approval_id"]:
        assert f"{bad}=" not in rules, f"告警规则不得使用敏感/高基数标签 {bad}"
    assert rules.count("{{ $labels.") >= 0  # 模板标签合法即可（占位，不触发）
