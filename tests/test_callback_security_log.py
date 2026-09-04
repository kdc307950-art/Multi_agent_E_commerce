"""回调端点「审计安全」验收测试（t4：平台级安全日志，不写不可信 tenant 审计）。

目标（见《Agent 宪法》「绝不信任客户端身份」）：
- 验签失败 / 非法 JSON / 未知 execution_id / 缺失身份或 nonce 等「不可信/未知」情形：
  **只写平台级安全日志**（`get_logger("security")`，字段 channel/client_ip/reason/
  execution_id_present/provider），**绝不**用请求 body 的不可信 tenant_id 写租户审计，
  **绝不**写数据库 audit。
- 签名验签通过且定位到可信执行记录的情形：租户审计已在 `store.apply_callback_atomic`
  同一事务内用执行记录的**可信 tenant_id** 写入（user_id="callback"）；端点只返回 HTTP，
  不重复用 body tenant_id 补审计。

HTTP 语义：signature_invalid→401、not_found→404、其余（含 bad_payload/missing_identity/
missing_nonce/重放/冲突/中间态）→200 applied=False、确认→200 applied=True。

本文件仅依赖后端无关的 MemoryStore（不需要 PostgreSQL），本机可直接 `pytest` 运行。
"""
from __future__ import annotations

import json
import logging
import time

import pytest
from fastapi.testclient import TestClient

from src.config import Settings
from src.core.types import PendingAction, Role
from src.execution import ExecutionMode, ExecutionStatus, build_callback_signature
from src.execution.verification import sign_payload
from src.infrastructure.store import MemoryStore
from src.llm.mock import MockLLM
from src.main import create_app
from src.observability.logging import PIIRedactionFilter

TEST_SECRET = "callback-security-test-secret"


# ---------------------------------------------------------------------------
# 捕获 security logger（与 test_observability 的 capture 模式一致，隔离该 logger）
# ---------------------------------------------------------------------------
class _CaptureHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.fixture
def security_log_capture():
    """捕获 `get_logger("security")` 的告警，测试结束清理 handler（不污染其它用例）。"""
    logger = logging.getLogger("security")
    prev_level = logger.level
    prev_propagate = logger.propagate
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    handler = _CaptureHandler()
    handler.addFilter(PIIRedactionFilter())
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)
        logger.propagate = prev_propagate


# ---------------------------------------------------------------------------
# 构造受控 api 应用（非受限 env，回调密钥固定，信任代理深度 1）
# ---------------------------------------------------------------------------
def _app(secret: str = TEST_SECRET):
    store = MemoryStore()
    store.create_tenant("TENANT-A", "租户A")
    store.create_tenant("TENANT-B", "租户B")
    store.add_membership("TENANT-A", "USER-001", Role.CUSTOMER)
    store.add_membership("TENANT-B", "USER-B1", Role.CUSTOMER)
    settings = Settings(
        env="test",
        execution_callback_hmac_secret=secret,
        login_trusted_proxy_depth=1,
    )
    app = create_app(store=store, llm=MockLLM(), seed=False, settings=settings)
    return store, app


def _make_submitted(store: MemoryStore, tenant: str, rid: str, amount: float = 100.0):
    """创建一笔 live 已提交（SUBMITTED）的执行记录（确定性，不依赖 provider 提交流）。"""
    op = store.create_operation(tenant, "th", "ORD-001", PendingAction.REFUND,
                                f"opkey-{rid}", time.time())
    rec = store.create_execution_record(
        tenant, operation_id=op.operation_id, pending_action=PendingAction.REFUND,
        order_id="ORD-001", idempotency_key=op.idempotency_key,
        mode=ExecutionMode.SHADOW, amount=amount, now=time.time())
    store.update_execution_record(tenant, rec.execution_id, status=ExecutionStatus.SUBMITTED,
                                  external_txn_id=f"txn-{rid}", submitted_at=time.time())
    return op, rec


def _cb(secret: str, tenant: str, execution_id: str, nonce: str = "n1",
        status: str = "succeeded", amount: float = 100.0, **extra):
    payload = {"tenant_id": tenant, "execution_id": execution_id,
               "external_txn_id": "txn-x", "status": status,
               "amount": amount, "nonce": nonce, **extra}
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return body, build_callback_signature(secret, payload)


def _audit_counts(store: MemoryStore) -> tuple[int, int]:
    return len(store.list_audit("TENANT-A")), len(store.list_audit("TENANT-B"))


# ---------------------------------------------------------------------------
# 1) 验签失败 → 401，只写平台安全日志，零租户审计
# ---------------------------------------------------------------------------
def test_signature_invalid_401_and_only_security_log(security_log_capture):
    store, app = _app()
    before_a, before_b = _audit_counts(store)
    body = json.dumps({"tenant_id": "TENANT-A", "execution_id": "e-unknown",
                       "nonce": "n1"}, separators=(",", ":")).encode("utf-8")
    with TestClient(app) as c:
        r = c.post("/api/callbacks/payment", content=body,
                   headers={"X-Signature": "deadbeef"})
        assert r.status_code == 401
        assert r.json().get("detail") == "回调签名校验失败"
    # 零租户审计写入（不可信来源不污染审计链）。
    assert _audit_counts(store) == (before_a, before_b)
    # 平台安全日志已写（含 channel/reason 等有界字段，不含 tenant_id）。
    msgs = security_log_capture.messages
    assert msgs, "验签失败应写平台安全日志"
    assert "payment-callback" in msgs[0]
    assert "tenant_id" not in msgs[0]


# ---------------------------------------------------------------------------
# 2) 非法 JSON（合法签名但 body 无法解析）→ 200 applied=False，零租户审计，只写日志
# ---------------------------------------------------------------------------
def test_bad_payload_only_security_log(security_log_capture):
    store, app = _app()
    before_a, before_b = _audit_counts(store)
    # 对非法 JSON 字节做合法签名：engine 验签通过 → json.loads 失败 → bad_payload。
    raw = b'{"tenant_id": "TENANT-A", "execution_id": "e-1", "nonce": "n1", "broken"'
    sig = sign_payload(TEST_SECRET, raw)
    with TestClient(app) as c:
        r = c.post("/api/callbacks/payment", content=raw, headers={"X-Signature": sig})
        assert r.status_code == 200
        assert r.json().get("applied") is False
        assert r.json().get("reason") == "bad_payload"
    assert _audit_counts(store) == (before_a, before_b)
    assert security_log_capture.messages, "非法 JSON 应写平台安全日志"


# ---------------------------------------------------------------------------
# 3) 未知 execution_id（含跨租户伪造）→ 404，零租户审计，只写日志
# ---------------------------------------------------------------------------
def test_unknown_execution_not_found_404_zero_audit(security_log_capture):
    store, app = _app()
    before_a, before_b = _audit_counts(store)
    # 合法签名 + 不存在/他人 execution_id（TENANT-B 带 TENANT-A 的 eid 伪造）。
    body, sig = _cb(TEST_SECRET, "TENANT-B", "e-does-not-exist", nonce="n1")
    with TestClient(app) as c:
        r = c.post("/api/callbacks/payment", content=body, headers={"X-Signature": sig})
        assert r.status_code == 404
        assert r.json().get("detail") == "执行记录不存在"
    # 零租户审计（不泄露存在性，不污染审计链）。
    assert _audit_counts(store) == (before_a, before_b)
    assert security_log_capture.messages, "未知 execution 应写平台安全日志"


# ---------------------------------------------------------------------------
# 4) 缺失身份（无 tenant_id/execution_id）→ 200 applied=False，零租户审计
# ---------------------------------------------------------------------------
def test_missing_identity_zero_audit(security_log_capture):
    store, app = _app()
    before_a, before_b = _audit_counts(store)
    # body 缺失 execution_id（身份不全）→ missing_identity。
    payload = {"tenant_id": "TENANT-A", "nonce": "n1"}
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sig = build_callback_signature(TEST_SECRET, payload)
    with TestClient(app) as c:
        r = c.post("/api/callbacks/payment", content=body, headers={"X-Signature": sig})
        assert r.status_code == 200
        assert r.json().get("reason") == "missing_identity"
    assert _audit_counts(store) == (before_a, before_b)


# ---------------------------------------------------------------------------
# 5) 缺失 nonce → 200 applied=False，零租户审计
# ---------------------------------------------------------------------------
def test_missing_nonce_zero_audit(security_log_capture):
    store, app = _app()
    before_a, before_b = _audit_counts(store)
    body, sig = _cb(TEST_SECRET, "TENANT-A", "e-1", nonce="")
    with TestClient(app) as c:
        r = c.post("/api/callbacks/payment", content=body, headers={"X-Signature": sig})
        assert r.status_code == 200
        assert r.json().get("reason") == "missing_nonce"
    assert _audit_counts(store) == (before_a, before_b)


# ---------------------------------------------------------------------------
# 6) 可信确认：审计由 store 同事务用执行记录可信 tenant_id 写入，端点不重复补审计
# ---------------------------------------------------------------------------
def test_confirmed_audit_uses_record_tenant_not_body():
    store, app = _app()
    _op, rec = _make_submitted(store, "TENANT-A", "CONFIRM-AUDIT", amount=100.0)
    body, sig = _cb(TEST_SECRET, "TENANT-A", rec.execution_id, nonce="n-AUDIT")
    with TestClient(app) as c:
        r = c.post("/api/callbacks/payment", content=body, headers={"X-Signature": sig})
        assert r.status_code == 200
        assert r.json().get("applied") is True
        assert r.json().get("reason") == "confirmed"
    audits = store.list_audit("TENANT-A")
    cb_audits = [a for a in audits if a.action.startswith("execution.callback.")]
    # 只有一条由 store 原子方法同事务写入的审计，tenant_id=执行记录可信租户，user_id="callback"。
    assert len(cb_audits) == 1
    assert cb_audits[0].action == "execution.callback.confirmed"
    assert cb_audits[0].tenant_id == "TENANT-A"   # 可信租户（执行记录），非 body
    assert cb_audits[0].user_id == "callback"      # 回调系统身份
    assert cb_audits[0].target_id == rec.execution_id
    # 端点未用 body tenant_id 追加第二条审计：TENANT-B 无任何回调审计。
    assert not [a for a in store.list_audit("TENANT-B")
                if a.action.startswith("execution.callback.")]
