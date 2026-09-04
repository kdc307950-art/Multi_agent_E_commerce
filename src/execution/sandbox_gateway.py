"""自部署「网关沙箱」服务（完全自托管，不触真实资金）。

这是一个**独立的、可运行的本地/内网 HTTP 服务**，模拟真实电商/支付网关的对外最小面：
服务端以 SQLite 文件做**持久化幂等**（绝不依赖进程内内存 dict），从而对同租户同
idempotency_key 的重复 submit 返回同一 external_txn_id / 同一结果，绝不重复扣款/退款；
跨租户同 idempotency_key 互不覆盖（以 (tenant_id, idempotency_key) 为唯一键）。

- submit   ：提交一笔执行（退款/退货/改址登记），服务端幂等。
- query    ：按 external_txn_id 查最终状态（供对账）。
- compensate：以 (tenant_id, idempotency_key, execution_id) 派生稳定 reversal_id，重放一致。

故障注入（供后续端到端验证与故障注入测试复用）：
通过请求头 ``X-Sandbox-Fault``（见 sandbox_faults.py）声明本次模拟的故障，例如：
- timeout：延迟超过调用方 deadline（客户端应收到 ProviderError(timeout)）；
- http_500 / http_503：网关返回对应 5xx（客户端应收到 ProviderError(upstream_5xx)）；
- signature_error / duplicate_notify：为回调链路预留（后续端到端任务使用），本服务先提供接缝。

凭证：沙箱网关的 API Key 由环境变量 ``SANDBOX_GATEWAY_API_KEY`` 注入（禁止硬编码明文）。
为空表示本地开发放行（仅内网/本地可用）；生产/预发布应配置。
"""
from __future__ import annotations

import os
import sqlite3
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException

from src.execution.sandbox_faults import (
    SANDBOX_FAULT_HEADER,
    parse_sandbox_fault_header,
    resolve_delay_ms,
    SandboxFault,
)


# ---------------------------------------------------------------------------
# SQLite 持久化（服务端幂等绝不依赖内存 dict）
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS gateway_txns (
    tenant_id       TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    external_txn_id TEXT NOT NULL,
    operation_id    TEXT NOT NULL,
    pending_action  TEXT NOT NULL,
    order_id        TEXT NOT NULL,
    amount          REAL    NOT NULL,
    status          TEXT    NOT NULL,
    receipt         TEXT    NOT NULL,
    created_at      REAL    NOT NULL,
    PRIMARY KEY (tenant_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS gateway_reversals (
    tenant_id       TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    execution_id    TEXT NOT NULL,
    reversal_id     TEXT NOT NULL,
    status          TEXT NOT NULL,
    receipt         TEXT NOT NULL,
    created_at      REAL    NOT NULL,
    PRIMARY KEY (tenant_id, idempotency_key, execution_id)
);
CREATE INDEX IF NOT EXISTS idx_gateway_txns_tenant_txn ON gateway_txns (tenant_id, external_txn_id);
"""


class GatewayStore:
    """沙箱网关的服务端持久化存储（SQLite）。所有写以唯一约束做原子幂等。"""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # WAL 提升并发读；同步写由唯一约束 + 事务保证幂等。
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.executescript(_SCHEMA)
            conn.commit()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=10.0)

    # ---- submit（服务端持久化幂等） ----
    def submit(self, *, tenant_id: str, idempotency_key: str, operation_id: str,
               pending_action: str, order_id: str, amount: float,
               default_status: str = "succeeded") -> dict:
        """提交一笔执行。同 (tenant_id, idempotency_key) 重复提交 → 返回既有记录。

        以数据库唯一约束 + 事务内「先查后插 + ON CONFLICT DO NOTHING」保证幂等：
        即使并发发送两个相同键的请求，也只会落一行，返回同一 external_txn_id / 结果。
        """
        ext_txn = "txn-" + uuid.uuid4().hex[:16]
        status = default_status
        receipt = {
            "external_txn_id": ext_txn,
            "order_id": order_id,
            "amount": amount,
            "status": status,
            "pending_action": pending_action,
            "provider": "sandbox-gateway",
            "ts": time.time(),
        }
        with self._conn() as conn:
            # 幂等：先做一次唯一约束防重（原子），再读回既有（重放一致）。
            conn.execute(
                """
                INSERT INTO gateway_txns (tenant_id, idempotency_key, external_txn_id,
                    operation_id, pending_action, order_id, amount, status, receipt, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                """,
                (tenant_id, idempotency_key, ext_txn, operation_id, pending_action,
                 order_id, amount, status, _json(receipt), time.time()),
            )
            row = conn.execute(
                "SELECT external_txn_id, status, receipt FROM gateway_txns "
                "WHERE tenant_id=? AND idempotency_key=?",
                (tenant_id, idempotency_key),
            ).fetchone()
        return {"external_txn_id": row[0], "status": row[1], "receipt": _unjson(row[2])}

    # ---- query（供对账） ----
    def query(self, *, tenant_id: str, external_txn_id: str) -> dict:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT external_txn_id, status, receipt, amount FROM gateway_txns "
                "WHERE tenant_id=? AND external_txn_id=?",
                (tenant_id, external_txn_id),
            ).fetchone()
        if row is None:
            return {"external_txn_id": external_txn_id, "status": "not_found",
                    "receipt": {"provider": "sandbox-gateway"}, "amount": None}
        return {"external_txn_id": row[0], "status": row[1], "receipt": _unjson(row[2]),
                "amount": row[3]}

    # ---- compensate（派生稳定 reversal_id，重放一致） ----
    def compensate(self, *, tenant_id: str, execution_id: str, idempotency_key: str,
                   amount: float) -> dict:
        reversal_id = "rev-" + str(uuid.uuid5(
            uuid.NAMESPACE_URL, f"sandbox-gateway://{tenant_id}:{idempotency_key}:{execution_id}"
        )).replace("-", "")[:16]
        receipt = {
            "reversal_id": reversal_id,
            "execution_id": execution_id,
            "amount": amount,
            "status": "reversed",
            "provider": "sandbox-gateway",
        }
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO gateway_reversals (tenant_id, idempotency_key, execution_id,
                    reversal_id, status, receipt, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (tenant_id, idempotency_key, execution_id) DO NOTHING
                """,
                (tenant_id, idempotency_key, execution_id, reversal_id,
                 "reversed", _json(receipt), time.time()),
            )
            row = conn.execute(
                "SELECT reversal_id, status, receipt FROM gateway_reversals "
                "WHERE tenant_id=? AND idempotency_key=? AND execution_id=?",
                (tenant_id, idempotency_key, execution_id),
            ).fetchone()
        return {"status": "succeeded", "receipt": _unjson(row[2])}

    # ---- 测试辅助：断言某键只落一行（验证服务端幂等） ----
    def count_txns(self, tenant_id: str, idempotency_key: str) -> int:
        with self._conn() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM gateway_txns WHERE tenant_id=? AND idempotency_key=?",
                (tenant_id, idempotency_key),
            ).fetchone()[0]


def _json(obj: dict) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _unjson(raw: str) -> dict:
    import json
    return json.loads(raw)


def create_sandbox_gateway_app(db_path: str, api_key: str = "") -> FastAPI:
    """构造沙箱网关 FastAPI 应用。

    建议用 ``uvicorn`` 以独立进程运行（本地/内网）；也可在测试中直接用 ASGI 客户端。
    """
    store = GatewayStore(db_path)
    app = FastAPI(title="Sandbox Gateway", docs_url=None, redoc_url=None)

    def _authorize(x_api_key: str | None) -> None:
        # 生产/预发布应注入 SANDBOX_GATEWAY_API_KEY 并要求匹配；为空仅限本地放行。
        if api_key and x_api_key != api_key:
            raise HTTPException(status_code=401, detail="invalid gateway api key")

    def _maybe_fault(faults: dict[str, str]) -> None:
        if SandboxFault.HTTP_500.value in faults:
            raise HTTPException(status_code=500, detail="sandbox injected http_500")
        if SandboxFault.HTTP_503.value in faults:
            raise HTTPException(status_code=503, detail="sandbox injected http_503")
        delay = resolve_delay_ms(faults)
        if delay > 0:
            import time as _t
            _t.sleep(delay)

    @app.post("/api/sandbox/submit")
    def submit(payload: dict, x_sandbox_fault: str | None = Header(default=None),
               x_api_key: str | None = Header(default=None)) -> dict:
        _authorize(x_api_key)
        faults = parse_sandbox_fault_header(x_sandbox_fault)
        _maybe_fault(faults)
        tenant_id = str(payload.get("tenant_id") or "")
        idempotency_key = str(payload.get("idempotency_key") or "")
        operation_id = str(payload.get("operation_id") or "")
        pending_action = str(payload.get("pending_action") or "")
        order_id = str(payload.get("order_id") or "")
        amount = float(payload.get("amount") or 0.0)
        if not tenant_id or not idempotency_key or not operation_id or not pending_action:
            raise HTTPException(status_code=422, detail="missing required submit field")
        default_status = str(payload.get("default_status") or "succeeded")
        return store.submit(tenant_id=tenant_id, idempotency_key=idempotency_key,
                            operation_id=operation_id, pending_action=pending_action,
                            order_id=order_id, amount=amount, default_status=default_status)

    @app.get("/api/sandbox/query")
    def query(tenant_id: str, external_txn_id: str,
              x_sandbox_fault: str | None = Header(default=None),
              x_api_key: str | None = Header(default=None)) -> dict:
        _authorize(x_api_key)
        faults = parse_sandbox_fault_header(x_sandbox_fault)
        _maybe_fault(faults)
        return store.query(tenant_id=tenant_id, external_txn_id=external_txn_id)

    @app.post("/api/sandbox/compensate")
    def compensate(payload: dict, x_sandbox_fault: str | None = Header(default=None),
                   x_api_key: str | None = Header(default=None)) -> dict:
        _authorize(x_api_key)
        faults = parse_sandbox_fault_header(x_sandbox_fault)
        _maybe_fault(faults)
        tenant_id = str(payload.get("tenant_id") or "")
        execution_id = str(payload.get("execution_id") or "")
        idempotency_key = str(payload.get("idempotency_key") or "")
        amount = float(payload.get("amount") or 0.0)
        if not tenant_id or not execution_id or not idempotency_key:
            raise HTTPException(status_code=422, detail="missing required compensate field")
        return store.compensate(tenant_id=tenant_id, execution_id=execution_id,
                                idempotency_key=idempotency_key, amount=amount)

    return app


def run_sandbox_gateway(db_path: str = "./data/sandbox_gateway.db",
                        host: str = "127.0.0.1", port: int = 8010,
                        api_key: str = "") -> None:
    """以 uvicorn 在本地/内网运行沙箱网关。"""
    import uvicorn
    app = create_sandbox_gateway_app(db_path, api_key=api_key)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run_sandbox_gateway(
        db_path=os.environ.get("SANDBOX_GATEWAY_DB", "./data/sandbox_gateway.db"),
        host=os.environ.get("SANDBOX_GATEWAY_HOST", "127.0.0.1"),
        port=int(os.environ.get("SANDBOX_GATEWAY_PORT", "8010")),
        api_key=os.environ.get("SANDBOX_GATEWAY_API_KEY", ""),
    )
