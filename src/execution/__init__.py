"""资金/业务执行链（退款、退货、改址的真实执行面）。

职责（与《错误处理与回退机制》§3.2/3.6 对齐）：
- 业务唯一幂等键：执行记录以 operation_id 为键（幂等键 = 操作的业务唯一标识，绝不含重试次数）；
- 执行状态机：pending_submit → submitted → confirmed / failed → compensated / human_handoff，
  reconcile 阶段再收口；
- shadow 模式：第一轮只生成「待执行记录 + 模拟回执」，绝不调用真实资金接口；
  受控执行开关（execution_mode=live + 显式 FundsProvider）通过后开启真实调用；
- 回调验签：外部系统回调经 HMAC-SHA256 验签 + 非重放窗口，重复回调只重放不重复生效；
- 失败补偿：外部明确失败后按幂等键发起补偿（回滚）并留痕，补偿失败转人工；
- 对账任务：为非终态执行记录查询外部状态并收口，不一致标记 mismatch 转人工。

多租户：所有执行记录带 tenant_id；执行前强制订单归属/租户作用域；缺失上下文默认拒绝。
"""
from src.execution.types import (
    CallbackResult,
    ExecutionMode,
    ExecutionRecord,
    ExecutionStatus,
    ExecutionOutcome,
    ReconciliationResult,
)
from src.execution.provider import FundsProvider, MockFundsProvider, build_provider
from src.execution.verification import (
    build_callback_signature,
    sign_payload,
    verify_hmac_signature,
)
from src.execution.engine import ExecutionEngine

__all__ = [
    "CallbackResult",
    "ExecutionMode",
    "ExecutionRecord",
    "ExecutionStatus",
    "ExecutionOutcome",
    "ReconciliationResult",
    "FundsProvider",
    "MockFundsProvider",
    "build_provider",
    "build_callback_signature",
    "sign_payload",
    "verify_hmac_signature",
    "ExecutionEngine",
]
