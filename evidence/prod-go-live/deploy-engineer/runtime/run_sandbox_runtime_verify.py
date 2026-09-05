"""沙箱网关运行态验证脚本（在容器内对真实 sandbox-gateway 做 HTTP 验证）。

运行环境：
- 在 after-sales-prod-api 镜像内的 verify-runner 容器执行；
- 通过内网 `http://sandbox-gateway:8010` 访问真实沙箱网关（服务端 SQLite 持久化幂等）；
- 与 sandbox-gateway 共享同名 volume `/data`（sandbox_gateway.db），可直接用 GatewayStore
  复核「同租户同键只落一行 / 跨租户隔离」的服务端断言；
- 结果写入 `/out/runtime_verify_report.json`（host 侧 evidence 挂载）。

覆盖：幂等、跨租户隔离、并发同键、回调安全（验签/重放/终态封闭/未知/跨租户/篡改金额）、
故障注入（5xx/超时）、对账（overdue 收敛 / missing external 转人工）。
全部不触真实资金。
"""
from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace

from src.core.types import OperationStatus, PendingAction, Role
from src.execution import (
    ExecutionEngine,
    ExecutionMode,
    ExecutionStatus,
    SandboxHttpFundsProvider,
    build_callback_signature,
    ProviderError,
)
from src.execution.sandbox_gateway import GatewayStore
from src.execution.sandbox_faults import SandboxFault
from src.infrastructure.store import MemoryStore

BASE_URL = os.environ.get("GATEWAY_BASE_URL", "http://sandbox-gateway:8010")
API_KEY = os.environ.get("GATEWAY_API_KEY", "verify-gateway-key")
CB_SECRET = os.environ.get("CALLBACK_HMAC_SECRET", "verify-callback-secret")
DB_PATH = os.environ.get("VERIFY_DB", "/data/sandbox_gateway.db")
OUT = os.environ.get("VERIFY_OUT", "/out/runtime_verify_report.json")

REPORT: dict = {}


def _mk(name: str):
    """创建当前报告分组。"""
    REPORT.setdefault(name, {})
    return REPORT[name]


def _sign_body(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _seed(store: MemoryStore) -> None:
    store.create_tenant("TENANT-A", "租户A")
    store.create_tenant("TENANT-B", "租户B")
    store.add_membership("TENANT-A", "USER-001", Role.CUSTOMER)
    store.add_membership("TENANT-B", "USER-B1", Role.CUSTOMER)


def _op(store, tenant, rid, action=PendingAction.REFUND):
    return store.create_operation(tenant, "th", "ORD-001", action,
                                  f"opkey:{rid}", time.time())


def _make_submitted(store, tenant, rid, ext_txn, amount=299.0):
    """直接在 store 造一笔 SUBMITTED 执行记录（确定性）。"""
    op = _op(store, tenant, rid)
    rec = store.create_execution_record(
        tenant, operation_id=op.operation_id, pending_action=PendingAction.REFUND,
        order_id="ORD-001", idempotency_key=op.idempotency_key, mode=ExecutionMode.LIVE,
        amount=amount, now=time.time())
    rec = store.update_execution_record(
        tenant, rec.execution_id, status=ExecutionStatus.SUBMITTED,
        external_txn_id=ext_txn, submitted_at=time.time() - 40.0)  # overdue
    return op, rec


# ===========================================================================
# 1) 幂等：同租户同键重复提交 → 同外部队列号；跨租户同键互不覆盖；服务端单行
# ===========================================================================
def verify_idempotency():
    g = _mk("idempotency")
    prov = SandboxHttpFundsProvider(BASE_URL, api_key=API_KEY, timeout=6.0)
    kw = dict(tenant_id="TENANT-A", idempotency_key="rt-key-aaa", operation_id="OP-RT-A",
              pending_action="refund", order_id="ORD-001", amount=299.0)
    r1 = prov.submit(**kw)
    r2 = prov.submit(**kw)
    g["same_tenant_same_key_external_txn_same"] = (r1["external_txn_id"] == r2["external_txn_id"])
    g["status_both_succeeded"] = (r1["status"] == r2["status"] == "succeeded")
    g["amount_is_order_paid_299"] = (r1["receipt"]["amount"] == 299.0)
    g["external_txn_id"] = r1["external_txn_id"]

    # 跨租户同键：必须不同外部队列号，各自 query 隔离。
    kwA = dict(tenant_id="TENANT-A", idempotency_key="rt-key-shared", operation_id="OP-SH-A",
               pending_action="refund", order_id="ORD-A", amount=10.0)
    kwB = dict(tenant_id="TENANT-B", idempotency_key="rt-key-shared", operation_id="OP-SH-B",
               pending_action="refund", order_id="ORD-B", amount=20.0)
    ra = prov.submit(**kwA)
    rb = prov.submit(**kwB)
    g["cross_tenant_same_key_external_txn_differ"] = (ra["external_txn_id"] != rb["external_txn_id"])
    qa = prov.query(tenant_id="TENANT-A", external_txn_id=ra["external_txn_id"])
    qb = prov.query(tenant_id="TENANT-B", external_txn_id=rb["external_txn_id"])
    g["cross_tenant_query_amount_a_10"] = (qa["amount"] == 10.0)
    g["cross_tenant_query_amount_b_20"] = (qb["amount"] == 20.0)
    g["cross_tenant_query_not_found"] = (prov.query(tenant_id="TENANT-B",
                                                   external_txn_id=ra["external_txn_id"])["status"] == "not_found")

    # 并发 16 线程同键：全部落到同一外部队列号（服务端唯一约束）。
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(prov.submit, **kw) for _ in range(16)]
        results = [f.result() for f in futs]
    g["concurrent_16_same_key_unique_txn"] = (len({r["external_txn_id"] for r in results}) == 1)

    # 服务端单行 + 跨租户单行（直接读共享 SQLite，复核唯一约束）。
    store = GatewayStore(DB_PATH)
    g["server_side_single_row"] = (store.count_txns("TENANT-A", "rt-key-aaa") == 1)
    g["server_side_single_row_cross_tenant_A"] = (store.count_txns("TENANT-A", "rt-key-shared") == 1)
    g["server_side_single_row_cross_tenant_B"] = (store.count_txns("TENANT-B", "rt-key-shared") == 1)
    prov.close()


# ===========================================================================
# 2) 回调安全：正确/错误 HMAC、重放、终态封闭、未知 execution、跨租户、篡改金额
# ===========================================================================
def verify_callback_security():
    g = _mk("callback_security")
    store = MemoryStore()
    _seed(store)
    op, rec = _make_submitted(store, "TENANT-A", "cb-sec", "txn-cb-sec")
    engine = ExecutionEngine(store, mode=ExecutionMode.LIVE, provider=None,
                             callback_secret=CB_SECRET, confirm_timeout_seconds=900.0)
    base = {"tenant_id": "TENANT-A", "execution_id": rec.execution_id,
            "external_txn_id": rec.external_txn_id, "status": "succeeded",
            "amount": rec.amount, "nonce": "nonce-sec"}

    r_correct = engine.apply_callback(_sign_body(base), build_callback_signature(CB_SECRET, base))
    g["correct_hmac_confirmed"] = (r_correct.applied is True and r_correct.status == "confirmed")

    r_wrong = engine.apply_callback(_sign_body(base), "deadbeef")
    g["wrong_hmac_signature_invalid"] = (r_wrong.reason == "signature_invalid" and r_wrong.applied is False)

    r_replay = engine.apply_callback(_sign_body(base), build_callback_signature(CB_SECRET, base))
    g["replay_same_nonce_replay"] = (r_replay.reason == "replay" and r_replay.applied is False)

    p_new = {**base, "nonce": "nonce-sec-new"}
    r_term = engine.apply_callback(_sign_body(p_new), build_callback_signature(CB_SECRET, p_new))
    g["terminal_locked_new_nonce"] = (r_term.reason == "terminal_locked" and r_term.applied is False)

    # 未知 execution（合法签名）→ not_found。
    p_unk = {"tenant_id": "TENANT-A", "execution_id": "e-does-not-exist",
             "external_txn_id": "txn", "status": "succeeded", "amount": 1.0, "nonce": "n-unk"}
    r_unk = engine.apply_callback(_sign_body(p_unk), build_callback_signature(CB_SECRET, p_unk))
    g["unknown_execution_not_found"] = (r_unk.reason == "not_found" and r_unk.applied is False)

    # 跨租户回调：body tenant=TENANT-B 但 execution 属于 TENANT-A → not_found（不泄露存在性）。
    p_cross = {"tenant_id": "TENANT-B", "execution_id": rec.execution_id,
               "external_txn_id": rec.external_txn_id, "status": "succeeded",
               "amount": rec.amount, "nonce": "n-cross"}
    r_cross = engine.apply_callback(_sign_body(p_cross), build_callback_signature(CB_SECRET, p_cross))
    g["cross_tenant_callback_not_found"] = (r_cross.reason == "not_found" and r_cross.applied is False)

    # 篡改金额（合法签名但 amount 与实际不符）→ amount_mismatch 转人工。
    g2 = MemoryStore()
    _seed(g2)
    op2, rec2 = _make_submitted(g2, "TENANT-A", "cb-tamper", "txn-tamper")
    eng2 = ExecutionEngine(g2, mode=ExecutionMode.LIVE, provider=None,
                           callback_secret=CB_SECRET, confirm_timeout_seconds=900.0)
    p_t = {"tenant_id": "TENANT-A", "execution_id": rec2.execution_id,
           "external_txn_id": rec2.external_txn_id, "status": "succeeded",
           "amount": 999999.0, "nonce": "n-tamper"}  # 与实付 299 不符
    r_tamper = eng2.apply_callback(_sign_body(p_t), build_callback_signature(CB_SECRET, p_t))
    g["tampered_amount_amount_mismatch"] = (r_tamper.reason == "amount_mismatch" and r_tamper.applied is False)


# ===========================================================================
# 3) 故障注入：http_500 / http_503 / timeout → 收敛为 ProviderError
# ===========================================================================
def verify_fault_injection():
    g = _mk("fault_injection")
    kw = dict(tenant_id="TENANT-A", idempotency_key="rt-key-fault", operation_id="OP-F",
              pending_action="refund", order_id="ORD-001", amount=100.0)
    try:
        p = SandboxHttpFundsProvider(BASE_URL, api_key=API_KEY, timeout=6.0,
                                     faults=[SandboxFault.HTTP_500])
        p.submit(**kw)
        g["http_500"] = "NO_ERROR"
    except ProviderError as e:
        g["http_500"] = e.code
    try:
        p = SandboxHttpFundsProvider(BASE_URL, api_key=API_KEY, timeout=6.0,
                                     faults=[SandboxFault.HTTP_503])
        p.submit(**kw)
        g["http_503"] = "NO_ERROR"
    except ProviderError as e:
        g["http_503"] = e.code
    try:
        p = SandboxHttpFundsProvider(BASE_URL, api_key=API_KEY, timeout=0.4,
                                     faults=[SandboxFault.TIMEOUT])
        p.query(tenant_id="TENANT-A", external_txn_id="txn-timeout")
        g["timeout"] = "NO_ERROR"
    except ProviderError as e:
        g["timeout"] = e.code
    # 网关 down：指向一个不存在的端口（模拟网络不可达）。
    try:
        p = SandboxHttpFundsProvider("http://127.0.0.1:1", api_key=API_KEY, timeout=1.0)
        p.submit(**kw)
        g["gateway_down"] = "NO_ERROR"
    except ProviderError as e:
        g["gateway_down"] = e.code


# ===========================================================================
# 4) 对账：overdue 未确认 + 网关真实 succeeded → reconcile 收敛 confirmed；
#        外部缺失（not_found）→ mismatch → 转人工
# ===========================================================================
def verify_reconcile():
    g = _mk("reconcile")
    store = MemoryStore()
    _seed(store)
    prov = SandboxHttpFundsProvider(BASE_URL, api_key=API_KEY, timeout=6.0)
    engine = ExecutionEngine(store, mode=ExecutionMode.LIVE, provider=prov,
                             callback_secret=CB_SECRET, confirm_timeout_seconds=30.0)

    # 已提交到网关的真实 external_txn_id + overdue → 对账经 query 网关确认为 succeeded → confirmed。
    ext = prov.submit(tenant_id="TENANT-A", operation_id="OP-REC", pending_action="refund",
                      order_id="ORD-001", amount=299.0, idempotency_key="rt-key-reconcile")["external_txn_id"]
    op, rec = _make_submitted(store, "TENANT-A", "reconcile-ok", ext, amount=299.0)
    res_ok = engine.reconcile("TENANT-A")
    rec_ok = store.get_execution_record("TENANT-A", rec.execution_id)
    g["overdue_reconcile_confirmed"] = (rec_ok.status is ExecutionStatus.CONFIRMED)
    g["reconcile_confirmed_count_ge_1"] = (res_ok.reconciled >= 1)

    # 外部缺失（external_txn_id 不存在于网关）→ 对账 unknown_provider_status:not_found → mismatch → human。
    store2 = MemoryStore()
    _seed(store2)
    eng2 = ExecutionEngine(store2, mode=ExecutionMode.LIVE, provider=prov,
                           callback_secret=CB_SECRET, confirm_timeout_seconds=30.0)
    _op2, rec2 = _make_submitted(store2, "TENANT-A", "reconcile-missing", "txn-does-not-exist")
    res_m = eng2.reconcile("TENANT-A")
    rec2 = store2.get_execution_record("TENANT-A", rec2.execution_id)
    g["missing_external_reconcile_mismatch"] = (rec2.status is ExecutionStatus.MISMATCHED)
    g["missing_external_operation_human_handoff"] = (
        store2.get_operation("TENANT-A", _op2.operation_id).status is OperationStatus.HUMAN_HANDOFF)
    g["reconcile_mismatch_count_ge_1"] = (res_m.mis_matched >= 1)

    # 审计可追溯：confirmed 路径与 mismatch 路径各落一条 token 级审计，并携带关联键。
    aud_ok = [a for a in store.search_audit("TENANT-A") if a.action == "execution.reconcile.confirmed"]
    g["audit_confirmed_action_written"] = len(aud_ok) >= 1
    g["audit_confirmed_carries_operation_id"] = any(
        (a.detail or {}).get("operation_id") == op.operation_id for a in aud_ok)
    aud_mm = [a for a in store2.search_audit("TENANT-A") if a.action == "execution.reconcile.mismatch"]
    g["audit_mismatch_action_written"] = len(aud_mm) >= 1
    g["audit_mismatch_carries_reason"] = any(
        (a.detail or {}).get("reason") == "missing_external_txn_id" or
        "unknown_provider_status" in (a.detail or {}).get("reason", "")
        for a in aud_mm)

    # 权重线索关联：执行记录/操作在收敛后的关联键是否齐全（对账口径所需的可追踪字段）。
    def _linkage(store, exec_id):
        r = store.get_execution_record("TENANT-A", exec_id)
        return {
            "tenant_id": r.tenant_id,
            "operation_id": r.operation_id,
            "external_operation_id": r.external_txn_id,
            "execution_status": r.status.value,
        }
    g["traceability_fields_confirmed_record"] = _linkage(store, rec.execution_id)
    g["traceability_fields_mismatched_record"] = _linkage(store2, rec2.execution_id)
    prov.close()


def main():
    t0 = time.time()
    # 先确认网关可达（用 /api/sandbox/query 触发一次真实连接）。
    import httpx
    try:
        # trust_env=False：禁用宿主进程可能注入的 HTTP(S)/SOCKS 代理环境变量，确保走容器内网直连。
        resp = httpx.get(f"{BASE_URL}/api/sandbox/query",
                         params={"tenant_id": "TENANT-A", "external_txn_id": "probe"},
                         headers={"X-API-Key": API_KEY}, timeout=3.0,
                         trust_env=False)
        REPORT["gateway_reachable_via_internal_http"] = (resp.status_code == 200)
    except Exception as e:  # noqa: BLE001
        REPORT["gateway_reachable_via_internal_http"] = False
        REPORT["gateway_reachable_error"] = str(e)

    verify_idempotency()
    verify_callback_security()
    verify_fault_injection()
    verify_reconcile()

    REPORT["gateway_base_url"] = BASE_URL
    REPORT["elapsed_seconds"] = round(time.time() - t0, 2)
    REPORT["note"] = ("与 sandbox-gateway 共卷读写 /data/sandbox_gateway.db 做服务端单行复核；"
                      "回调/对账用引擎+内存 store 复核状态机收敛语义。全部不触真实资金。")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(REPORT, fh, ensure_ascii=False, indent=2)
    print("RUNTIME_VERIFY_JSON=" + OUT)
    print("RESULT=" + json.dumps(REPORT, ensure_ascii=False))


if __name__ == "__main__":
    main()
