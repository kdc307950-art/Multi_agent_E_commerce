"""外部资金/业务系统的可插拔 Provider（完全自托管，不依赖任何第三方 SaaS）。

- FundsProvider：协议。生产实现接入项目自管的支付/电商网关，首次沙箱用 Mock 提供方；
- MockFundsProvider：确定性模拟提供方，用于 shadow/live 沙箱验收。它不产生真实资金副作用，
  只对幂等键派生稳定的外部队列号并返回回执，保证同幂等键重放得到同一外部队列号（不重复）；
- SandboxHttpFundsProvider：调用**自部署网关沙箱服务**（src.execution.sandbox_gateway）的
  非 mock HTTP 提供方。同租户同 idempotency_key 由沙箱服务端做持久化幂等（绝不依赖内存 dict），
  跨租户互不覆盖；故障注入通过 X-Sandbox-Fault 头声明（见 sandbox_faults.py）。
"""
from __future__ import annotations

import hashlib
import time
from typing import Optional, Protocol

import httpx

from src.execution.sandbox_faults import SANDBOX_FAULT_HEADER, build_sandbox_fault_header, SandboxFault


class ProviderError(Exception):
    """外部系统调用失败（网络/业务拒绝）。携带机器可读 code 供补偿/转人工。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class FundsProvider(Protocol):
    """外部资金/业务网关的最小调用面。

    实现必须：
    - submit 对同一 idempotency_key 幂等（重复提交返回同一 external_txn_id / 结果，不重复扣款）；
    - query 用于对账：按 external_txn_id 查询最终状态。
    """

    def submit(
        self,
        *,
        tenant_id: str,
        operation_id: str,
        pending_action: str,
        order_id: str,
        amount: float,
        idempotency_key: str,
    ) -> dict:
        """提交一笔执行，返回 {external_txn_id, status, receipt}；失败抛 ProviderError。"""
        ...

    def query(self, *, tenant_id: str, external_txn_id: str) -> dict:
        """查询外部队列号状态，返回 {external_txn_id, status, receipt, amount}。"""
        ...

    def compensate(self, *, tenant_id: str, execution_id: str, idempotency_key: str,
                  amount: float) -> dict:
        """对一笔执行发起补偿（回滚/反向操作），返回 {status, receipt}。"""
        ...


def _derive_txn_id(idempotency_key: str) -> str:
    """由业务幂等键派生稳定外部队列号（同幂等键同号，绝不因重试变化）。"""
    digest = hashlib.sha1(idempotency_key.encode("utf-8")).hexdigest()
    return "txn-" + digest[:16]


class MockFundsProvider:
    """确定性模拟提供方。

    - `result`：'success' | 'failure'，决定 submit/query 的最终状态（测试注入失败路径）；
    - 对同一 idempotency_key 的重复 submit 返回同一 external_txn_id（重放不重复）；
    - `query` 与 submit 结果一致，供对账收敛。
    """

    def __init__(self, *, result: str = "success") -> None:
        if result not in {"success", "failure"}:
            raise ValueError("result 必须是 'success' 或 'failure'")
        self._result = result
        self._submitted: dict[str, dict] = {}

    def submit(self, *, tenant_id: str, operation_id: str, pending_action: str,
               order_id: str, amount: float, idempotency_key: str) -> dict:
        external_txn_id = _derive_txn_id(idempotency_key)
        # 幂等：同一幂等键重复提交返回既有结果，不重复生成。
        if idempotency_key in self._submitted:
            return self._submitted[idempotency_key]
        status = "succeeded" if self._result == "success" else "failed"
        receipt = {
            "external_txn_id": external_txn_id,
            "order_id": order_id,
            "amount": amount,
            "status": status,
            "pending_action": pending_action,
            "provider": "mock-sandbox",
            "ts": time.time(),
        }
        record = {"external_txn_id": external_txn_id, "status": status, "receipt": receipt}
        self._submitted[idempotency_key] = record
        return record

    def query(self, *, tenant_id: str, external_txn_id: str) -> dict:
        status = "succeeded" if self._result == "success" else "failed"
        return {
            "external_txn_id": external_txn_id,
            "status": status,
            "receipt": {"external_txn_id": external_txn_id, "status": status,
                        "provider": "mock-sandbox"},
        }

    def compensate(self, *, tenant_id: str, execution_id: str, idempotency_key: str,
                  amount: float) -> dict:
        """模拟补偿：返回一条确定性反向回执（以 execution_id 派生稳定编号，重放一致）。"""
        reversal_id = "rev-" + hashlib.sha1(
            f"{idempotency_key}:{execution_id}".encode("utf-8")).hexdigest()[:16]
        receipt = {
            "reversal_id": reversal_id,
            "execution_id": execution_id,
            "amount": amount,
            "status": "reversed",
            "provider": "mock-sandbox",
        }
        return {"status": "succeeded", "receipt": receipt}


class SandboxHttpFundsProvider:
    """接入自部署网关沙箱服务的非 mock HTTP 提供方。

    - submit/query/compensate 均通过 HTTP 调用沙箱网关；沙箱端用 SQLite 做**服务端持久化幂等**：
      同租户同 idempotency_key 重复 submit 返回同一 external_txn_id / 结果，绝不重复扣款/退款；
      不同租户同 idempotency_key 以 (tenant_id, idempotency_key) 唯一键互不覆盖（租户级幂等）。
    - compensate 以 (tenant_id, idempotency_key, execution_id) 在服务端派生出稳定 reversal_id，
      重复发起返回同一回执（重放一致）。
    - 网络错误 / 超时 / 5xx → 抛 ProviderError，由引擎 fail-closed 转对账或人工，绝不静默放行。
    - 故障注入：`faults` 参数或 `fault_header` 直接声明本次要模拟的故障（见 sandbox_faults.py），
      供后续端到端验证与故障注入测试复用。`transport` 可注入（如 httpx.MockTransport）做单测。

    安全约束：base_url 指向完全自托管/本地的沙箱网关；api_key 由环境变量注入，禁止硬编码明文。
    """

    def __init__(self, base_url: str, *, api_key: str = "", timeout: float = 10.0,
                 transport: "httpx.BaseTransport | None" = None,
                 fault_header: str | None = None,
                 faults: list[SandboxFault] | list[str] | None = None) -> None:
        if not base_url:
            raise ProviderError("gateway_unconfigured", "sandbox_http 提供方必须配置 gateway_base_url")
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._api_key = api_key
        # 故障注入：构造 或 显式传入的 X-Sandbox-Fault 头（优先用显式头）。
        self._fault_header = fault_header or build_sandbox_fault_header(faults or [])
        self._client = httpx.Client(timeout=httpx.Timeout(timeout), trust_env=False, transport=transport)

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            h["X-API-Key"] = self._api_key
        if self._fault_header:
            h[SANDBOX_FAULT_HEADER] = self._fault_header
        return h

    def _post(self, path: str, payload: dict) -> dict:
        try:
            resp = self._client.post(f"{self._base_url}{path}", json=payload, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise ProviderError("timeout", f"网关超时: {exc}") from exc
        except httpx.TransportError as exc:
            raise ProviderError("network", f"网关不可达: {exc}") from exc
        if resp.status_code >= 500:
            raise ProviderError("upstream_5xx", f"网关返回 {resp.status_code}: {resp.text[:120]}")
        if resp.status_code >= 400:
            raise ProviderError("gateway_rejected", f"网关拒绝 {resp.status_code}: {resp.text[:120]}")
        try:
            return resp.json()
        except Exception as exc:
            raise ProviderError("bad_payload", f"网关返回非法载荷: {exc}") from exc

    def _get(self, path: str, params: dict) -> dict:
        try:
            resp = self._client.get(f"{self._base_url}{path}", params=params, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise ProviderError("timeout", f"网关超时: {exc}") from exc
        except httpx.TransportError as exc:
            raise ProviderError("network", f"网关不可达: {exc}") from exc
        if resp.status_code >= 500:
            raise ProviderError("upstream_5xx", f"网关返回 {resp.status_code}: {resp.text[:120]}")
        if resp.status_code >= 400:
            raise ProviderError("gateway_rejected", f"网关拒绝 {resp.status_code}: {resp.text[:120]}")
        try:
            return resp.json()
        except Exception as exc:
            raise ProviderError("bad_payload", f"网关返回非法载荷: {exc}") from exc

    def submit(self, *, tenant_id: str, operation_id: str, pending_action: str,
               order_id: str, amount: float, idempotency_key: str,
               default_status: str | None = None) -> dict:
        payload = {
            "tenant_id": tenant_id, "operation_id": operation_id,
            "pending_action": pending_action, "order_id": order_id,
            "amount": amount, "idempotency_key": idempotency_key,
        }
        # default_status 可选透传：供失败路径压测注入"明确失败"（沙箱端仍走服务端持久化幂等，
        # 同键重复提交返回同一 external_txn_id/同一结果）。缺省为 None → 沙箱默认 succeeded。
        if default_status is not None:
            payload["default_status"] = default_status
        return self._post("/api/sandbox/submit", payload)

    def query(self, *, tenant_id: str, external_txn_id: str) -> dict:
        return self._get("/api/sandbox/query", {"tenant_id": tenant_id,
                                                "external_txn_id": external_txn_id})

    def compensate(self, *, tenant_id: str, execution_id: str, idempotency_key: str,
                   amount: float) -> dict:
        return self._post("/api/sandbox/compensate", {
            "tenant_id": tenant_id, "execution_id": execution_id,
            "idempotency_key": idempotency_key, "amount": amount,
        })

    def close(self) -> None:
        self._client.close()


def build_provider(settings) -> Optional[FundsProvider]:
    """按配置构造外部提供方。支持 'mock' 与 'sandbox_http'（自部署网关沙箱）。

    - mock：确定性模拟提供方（不触真实接口）；
    - sandbox_http：调用自部署网关沙箱服务，需配置 gateway_base_url；缺失即 fail-closed。
    shadow 模式无需 provider（不触真实接口）；live 模式必须显式提供 provider，否则 fail-closed。
    """
    name = getattr(settings, "execution_provider", "mock")
    if name == "mock":
        return MockFundsProvider()
    if name == "sandbox_http":
        base_url = getattr(settings, "gateway_base_url", "") or ""
        if not base_url:
            raise ProviderError("gateway_unconfigured",
                                "execution_provider=sandbox_http 必须配置 gateway_base_url")
        return SandboxHttpFundsProvider(
            base_url=base_url,
            api_key=getattr(settings, "gateway_api_key", "") or "",
            timeout=float(getattr(settings, "gateway_timeout_seconds", 10.0) or 10.0),
        )
    raise ProviderError("unsupported_provider", f"未知执行提供方: {name}")
