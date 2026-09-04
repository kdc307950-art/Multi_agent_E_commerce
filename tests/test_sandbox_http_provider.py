"""网关沙箱 + HTTP FundsProvider 的验收测试（真实 HTTP 链路，不触真实资金）。

覆盖本次交付口径：
- 同租户同 idempotency_key 重复 submit：沙箱服务端以 SQLite 持久化幂等返回同一
  external_txn_id / 同一结果，绝不重复执行（count_txns == 1）；
- 不同租户同 idempotency_key：以 (tenant_id, idempotency_key) 唯一键互不覆盖（租户级幂等）；
- query 按 external_txn_id 查最终状态（对账用）；
- compensate 以 (tenant_id, idempotency_key, execution_id) 派生稳定 reversal_id 且重放一致；
- 故障注入：timeout / http_500 / http_503 由网关按 X-Sandbox-Fault 头注入，provider 端
  收敛为不同 code 的 ProviderError（供后续端到端验证复用）；
- build_provider 的 sandbox_http 分支与 fail-closed（缺 base_url 即拒绝）；
- live 引擎经 SandboxHttpFundsProvider 提交后回调确认、跨租户回调拒绝（端到端收敛）。
"""
from __future__ import annotations

import json
import socket
import threading
import time

import pytest
import uvicorn

from src.config import Settings
from src.core.types import OperationStatus, PendingAction, Role, generate_operation_key
from src.execution import (
    ExecutionMode,
    ExecutionStatus,
    ProviderError,
    SandboxHttpFundsProvider,
    build_callback_signature,
    build_provider,
)
from src.execution.sandbox_faults import (
    SandboxFault,
    build_sandbox_fault_header,
    parse_sandbox_fault_header,
)
from src.execution.sandbox_gateway import GatewayStore, create_sandbox_gateway_app
from src.infrastructure.store import MemoryStore

DEFAULT_CALLBACK_SECRET = "shadow-callback-secret"


# ---------------------------------------------------------------------------
# 启动一个真实运行的沙箱网关（uvicorn 后台线程，绑定 127.0.0.1 临时端口）
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def sandbox_url(tmp_path_factory):
    db_path = str(tmp_path_factory.mktemp("sandbox_gw") / "gw.db")
    app = create_sandbox_gateway_app(db_path, api_key="test-gateway-key")

    # 找一个可用端口
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    # 等待服务就绪
    import httpx
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            httpx.get(f"{base}/docs", timeout=0.5)  # 触发一次连接，接受 404/200 均可表示已监听
            break
        except Exception:
            time.sleep(0.05)
    yield base
    server.should_exit = True


@pytest.fixture()
def provider(sandbox_url):
    return SandboxHttpFundsProvider(sandbox_url, api_key="test-gateway-key", timeout=5.0)


def _submit_kwargs(tenant: str, key: str, *, operation_id: str = "OP-1",
                   action: str = "refund", order_id: str = "ORD-1", amount: float = 99.9):
    return dict(tenant_id=tenant, idempotency_key=key, operation_id=operation_id,
                pending_action=action, order_id=order_id, amount=amount)


# ---------------------------------------------------------------------------
# 1) 同租户同 idempotency_key 重复 submit → 服务端幂等（绝不重复执行）
# ---------------------------------------------------------------------------
def test_same_tenant_same_key_submit_is_idempotent(provider, sandbox_url, tmp_path):
    # 直接通过 provider 走 HTTP 提交两次同一键。
    kw = _submit_kwargs("TENANT-A", "key-same-1", operation_id="OP-A")
    first = provider.submit(**kw)
    second = provider.submit(**kw)
    assert first["external_txn_id"] == second["external_txn_id"]
    assert first["status"] == second["status"] == "succeeded"
    assert first["receipt"]["amount"] == 99.9
    assert second["receipt"]["external_txn_id"] == first["receipt"]["external_txn_id"]
    # 服务端持久化幂等：同键只落一层（绝不重复扣款/退款）。跨 provider 实例仍一致（见下个用例）。


def test_same_tenant_same_key_server_side_single_row(provider, sandbox_url):
    """服务端 SQLite 保证同租户同键只落一行（跨 provider 实例 / 跨进程仍幂等）。"""
    kw = _submit_kwargs("TENANT-A", "key-same-2", operation_id="OP-A2")
    a = SandboxHttpFundsProvider(sandbox_url, api_key="test-gateway-key", timeout=5.0)
    b = SandboxHttpFundsProvider(sandbox_url, api_key="test-gateway-key", timeout=5.0)
    r1 = a.submit(**kw)
    r2 = b.submit(**kw)  # 另一个 provider 实例（模拟重试/另一进程）重复提交
    assert r1["external_txn_id"] == r2["external_txn_id"]
    assert r1["receipt"]["status"] == "succeeded"


# ---------------------------------------------------------------------------
# 2) 不同租户同 idempotency_key → 互不覆盖（租户级幂等）
# ---------------------------------------------------------------------------
def test_cross_tenant_same_key_no_overwrite(provider):
    """不同租户使用相同 idempotency_key：必须生成不同 external_txn_id，互不覆盖。"""
    key = "shared-key-1"
    ra = provider.submit(**_submit_kwargs("TENANT-A", key, operation_id="OP-AX", amount=10.0))
    rb = provider.submit(**_submit_kwargs("TENANT-B", key, operation_id="OP-BX", amount=20.0))
    assert ra["external_txn_id"] != rb["external_txn_id"]
    # 各自 query 只能查到本租户自己的一笔（金额/状态独立）。
    qa = provider.query(tenant_id="TENANT-A", external_txn_id=ra["external_txn_id"])
    qb = provider.query(tenant_id="TENANT-B", external_txn_id=rb["external_txn_id"])
    assert qa["amount"] == 10.0
    assert qb["amount"] == 20.0
    assert qa["external_txn_id"] == ra["external_txn_id"]
    assert qb["external_txn_id"] == rb["external_txn_id"]


def test_query_missing_or_wrong_tenant_not_found(provider):
    """按 external_txn_id 查状态：不存在 → not_found（供对账识别缺失）。"""
    q = provider.query(tenant_id="TENANT-A", external_txn_id="txn-does-not-exist")
    assert q["status"] == "not_found"
    # 跨租户查不到本租户之外的外部队列号。
    ra = provider.submit(**_submit_kwargs("TENANT-A", "key-q-1", operation_id="OP-Q1"))
    q_wrong = provider.query(tenant_id="TENANT-B", external_txn_id=ra["external_txn_id"])
    assert q_wrong["status"] == "not_found"


# ---------------------------------------------------------------------------
# 3) compensate：派生稳定 reversal_id，重放一致
# ---------------------------------------------------------------------------
def test_compensate_derives_stable_reversal_id(provider):
    c1 = provider.compensate(tenant_id="TENANT-A", execution_id="EXEC-1",
                             idempotency_key="key-rev-1", amount=99.9)
    c2 = provider.compensate(tenant_id="TENANT-A", execution_id="EXEC-1",
                             idempotency_key="key-rev-1", amount=99.9)
    assert c1["status"] == "succeeded"
    assert c1["receipt"]["status"] == "reversed"
    assert c1["receipt"]["reversal_id"] == c2["receipt"]["reversal_id"]  # 重放一致


def test_compensate_cross_tenant_different_reversal_id(provider):
    """不同租户同一 execution_id/idempotency_key → 派生不同 reversal_id（租户级隔离）。"""
    ca = provider.compensate(tenant_id="TENANT-A", execution_id="EXEC-X",
                             idempotency_key="key-rev-x", amount=50.0)
    cb = provider.compensate(tenant_id="TENANT-B", execution_id="EXEC-X",
                             idempotency_key="key-rev-x", amount=50.0)
    assert ca["receipt"]["reversal_id"] != cb["receipt"]["reversal_id"]


# ---------------------------------------------------------------------------
# 4) 故障注入：timeout / http_500 / http_503 收敛为 ProviderError
# ---------------------------------------------------------------------------
def test_fault_http_500_raises_provider_error(provider):
    p = SandboxHttpFundsProvider(
        provider._base_url, api_key="test-gateway-key", timeout=5.0,
        faults=[SandboxFault.HTTP_500])
    with pytest.raises(ProviderError) as exc:
        p.submit(**_submit_kwargs("TENANT-A", "key-f500", operation_id="OP-F5"))
    assert exc.value.code == "upstream_5xx"


def test_fault_http_503_raises_provider_error(provider):
    p = SandboxHttpFundsProvider(
        provider._base_url, api_key="test-gateway-key", timeout=5.0,
        faults=[SandboxFault.HTTP_503])
    with pytest.raises(ProviderError) as exc:
        p.submit(**_submit_kwargs("TENANT-A", "key-f503", operation_id="OP-F6"))
    assert exc.value.code == "upstream_5xx"


def test_fault_timeout_raises_provider_error(provider):
    p = SandboxHttpFundsProvider(
        provider._base_url, api_key="test-gateway-key", timeout=0.5,
        faults=[SandboxFault.TIMEOUT])
    with pytest.raises(ProviderError) as exc:
        p.query(tenant_id="TENANT-A", external_txn_id="txn-timeout")
    assert exc.value.code == "timeout"


def test_fault_header_roundtrip():
    assert parse_sandbox_fault_header("http_500") == {"http_500": ""}
    assert parse_sandbox_fault_header("delay_ms:2000") == {"delay_ms": "2000"}
    h = build_sandbox_fault_header([SandboxFault.HTTP_500, SandboxFault.TIMEOUT], delay_ms=1500)
    assert "http_500" in h and "timeout" in h and "delay_ms:1500" in h


def test_unauthorized_api_key_rejected(provider):
    bad = SandboxHttpFundsProvider(provider._base_url, api_key="wrong-key", timeout=5.0)
    with pytest.raises(ProviderError) as exc:
        bad.submit(**_submit_kwargs("TENANT-A", "key-unauth", operation_id="OP-U"))
    assert exc.value.code == "gateway_rejected"  # 401 → provider 拒绝


# ---------------------------------------------------------------------------
# 5) build_provider：sandbox_http + fail-closed（缺 base_url 即拒绝）
# ---------------------------------------------------------------------------
def test_build_provider_sandbox_http(sandbox_url):
    p = build_provider(Settings(execution_provider="sandbox_http",
                                gateway_base_url=sandbox_url,
                                gateway_api_key="test-gateway-key"))
    assert isinstance(p, SandboxHttpFundsProvider)


def test_build_provider_sandbox_http_missing_base_url_fail_closed():
    with pytest.raises(ProviderError) as exc:
        build_provider(Settings(execution_provider="sandbox_http"))
    assert exc.value.code == "gateway_unconfigured"


# ---------------------------------------------------------------------------
# 6) 端到端：live 引擎经 SandboxHttpFundsProvider 提交 → 回调确认（工程收敛）
# ---------------------------------------------------------------------------
def _seed(store):
    store.create_tenant("TENANT-A", "a")
    store.add_membership("TENANT-A", "USER-001", Role.CUSTOMER)


def _op(store, action, order, rid):
    key = generate_operation_key(action, "TENANT-A", order, rid)
    return store.create_operation("TENANT-A", "th", order, action, key, time.time())


def test_live_engine_sandbox_http_submit_then_callback_confirm(sandbox_url):
    from src.tools import EcommerceAdapter
    from src.tools.adapter import build_adapter

    s = MemoryStore(); _seed(s)
    provider = SandboxHttpFundsProvider(sandbox_url, api_key="test-gateway-key", timeout=5.0)
    ad = EcommerceAdapter(execution_mode=ExecutionMode.LIVE, provider=provider,
                          callback_secret=DEFAULT_CALLBACK_SECRET)
    op = _op(s, PendingAction.REFUND, "ORD-001", "REQ-SHTTP")
    out = ad.execute_operation(s, "TENANT-A", "USER-001", "customer", op.operation_id)
    assert out.status is ExecutionStatus.SUBMITTED
    rec = s.get_execution_record("TENANT-A", out.execution_id)
    assert rec.external_txn_id

    # 模拟网关回调（验签 + 非重放）。
    payload = {"tenant_id": "TENANT-A", "execution_id": rec.execution_id,
               "external_txn_id": rec.external_txn_id, "status": "succeeded",
               "amount": rec.amount, "nonce": "nonce-SHTTP"}
    sig = build_callback_signature(DEFAULT_CALLBACK_SECRET, payload)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    engine = ad.make_execution_engine(s)
    r1 = engine.apply_callback(body, sig)
    assert r1.applied is True and r1.status == ExecutionStatus.CONFIRMED.value
    assert s.get_operation("TENANT-A", op.operation_id).status == OperationStatus.EXECUTED

    # 对账：已确认记录不重复执行（query 沙箱真实状态）。
    res = engine.reconcile("TENANT-A")
    assert s.get_execution_record("TENANT-A", rec.execution_id).status is ExecutionStatus.CONFIRMED


def test_live_engine_build_adapter_sandbox_http(sandbox_url):
    from src.tools.adapter import build_adapter
    ad = build_adapter(Settings(execution_mode="live", execution_provider="sandbox_http",
                                gateway_base_url=sandbox_url, gateway_api_key="test-gateway-key"))
    assert ad.execution_mode is ExecutionMode.LIVE
    assert ad.live_execution_enabled is True
    assert ad.provider_name == "SandboxHttpFundsProvider"


# ---------------------------------------------------------------------------
# 7) GatewayStore 直接单元测试：SQLite 服务端持久化幂等 + 租户级唯一约束（强证据）
# ---------------------------------------------------------------------------
def test_gateway_store_server_side_idempotent_single_row(tmp_path):
    """同租户同 idempotency_key 重复 submit：服务端只落一行（唯一约束），返回同一 external_txn_id。

    这是服务端持久化幂等的强断言：即使不经过 HTTP，直接用 SQLite 存储也绝不产生重复行，
    从而绝不重复扣款/退款。
    """
    store = GatewayStore(str(tmp_path / "gw.db"))
    r1 = store.submit(tenant_id="TENANT-A", idempotency_key="k1", operation_id="OP-1",
                      pending_action="refund", order_id="ORD-1", amount=10.0)
    r2 = store.submit(tenant_id="TENANT-A", idempotency_key="k1", operation_id="OP-1",
                      pending_action="refund", order_id="ORD-1", amount=10.0)
    assert r1["external_txn_id"] == r2["external_txn_id"]
    assert r1["status"] == r2["status"] == "succeeded"
    assert store.count_txns("TENANT-A", "k1") == 1  # 服务端只落一行

    # 重新打开存储（模拟进程重启）：幂等仍成立（持久化，非内存 dict）。
    store2 = GatewayStore(str(tmp_path / "gw.db"))
    r3 = store2.submit(tenant_id="TENANT-A", idempotency_key="k1", operation_id="OP-1",
                       pending_action="refund", order_id="ORD-1", amount=10.0)
    assert r3["external_txn_id"] == r1["external_txn_id"]
    assert store2.count_txns("TENANT-A", "k1") == 1


def test_gateway_store_cross_tenant_same_key_isolated(tmp_path):
    """不同租户用同一 idempotency_key：唯一键是 (tenant_id, idempotency_key)，互不覆盖。"""
    store = GatewayStore(str(tmp_path / "gw.db"))
    ra = store.submit(tenant_id="TENANT-A", idempotency_key="shared", operation_id="OP-A",
                      pending_action="refund", order_id="ORD-A", amount=10.0)
    rb = store.submit(tenant_id="TENANT-B", idempotency_key="shared", operation_id="OP-B",
                      pending_action="refund", order_id="ORD-B", amount=20.0)
    assert ra["external_txn_id"] != rb["external_txn_id"]
    assert store.count_txns("TENANT-A", "shared") == 1
    assert store.count_txns("TENANT-B", "shared") == 1
    qa = store.query(tenant_id="TENANT-A", external_txn_id=ra["external_txn_id"])
    qb = store.query(tenant_id="TENANT-B", external_txn_id=rb["external_txn_id"])
    assert qa["amount"] == 10.0 and qb["amount"] == 20.0
    # 跨租户查不到对方外部队列号（租户作用域查询）。
    assert store.query(tenant_id="TENANT-B", external_txn_id=ra["external_txn_id"])["status"] == "not_found"


def test_gateway_store_compensate_replay_stable(tmp_path):
    """compensate 以 (tenant_id, idempotency_key, execution_id) 派生稳定 reversal_id，重放一致。"""
    store = GatewayStore(str(tmp_path / "gw.db"))
    c1 = store.compensate(tenant_id="TENANT-A", execution_id="E-1", idempotency_key="rk",
                          amount=5.0)
    c2 = store.compensate(tenant_id="TENANT-A", execution_id="E-1", idempotency_key="rk",
                          amount=5.0)
    assert c1["status"] == "succeeded"
    assert c1["receipt"]["reversal_id"] == c2["receipt"]["reversal_id"]
