"""外部资金/业务系统的可插拔 Provider（完全自托管，不依赖任何第三方 SaaS）。

- FundsProvider：协议。生产实现接入项目自管的支付/电商网关，首次沙箱用 Mock 提供方；
- MockFundsProvider：确定性模拟提供方，用于 shadow/live 沙箱验收。它不产生真实资金副作用，
  只对幂等键派生稳定的外部队列号并返回回执，保证同幂等键重放得到同一外部队列号（不重复）。
"""
from __future__ import annotations

import hashlib
import time
from typing import Optional, Protocol


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


def build_provider(settings) -> Optional[FundsProvider]:
    """按配置构造外部提供方。当前仅沙箱 Mock；真实网关接入属后期替换点。

    shadow 模式无需 provider（不触真实接口）；live 模式必须显式提供 provider，否则 fail-closed。
    """
    name = getattr(settings, "execution_provider", "mock")
    if name == "mock":
        return MockFundsProvider()
    raise ProviderError("unsupported_provider", f"未知执行提供方: {name}")
