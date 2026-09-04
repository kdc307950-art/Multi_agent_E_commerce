"""执行链领域类型：执行模式、执行状态机、执行记录、执行结果与回调结果。

安全/幂等约定：
- ExecutionRecord 以 operation_id 为一对一锚点（幂等键复用 operation 的业务唯一幂等键），
  同一操作重放/审批重放/回调重放都只会得到同一执行记录，绝不重复提交外部系统。
- 状态机是单向的，外部只允许「合法前驱 → 合法后继」的跃迁（见 TRANSITIONS），
  非法跃迁一律拒绝并转人工，杜绝重复扣款/退款。
- mode 记录该执行是在 shadow（模拟回执，不触真实资金）还是 live（真实调用）下发起。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from src.core.types import OperationStatus, PendingAction


class ExecutionMode(str, Enum):
    """执行发起模式。shadow 为第一轮；live 为通过沙箱验收后的受控执行开关。"""

    SHADOW = "shadow"
    LIVE = "live"


class ExecutionStatus(str, Enum):
    """执行状态机（细粒度）。终态为 CONFIRMED / COMPENSATED / HUMAN_HANDOFF / MISMATCHED。

    其余为中间/异常态，交由补偿与对账任务收口：
    - PENDING_SUBMIT：待执行记录（shadow/live 第一帧，尚未提交外部）；
    - SUBMITTED：已提交外部系统，等待确认（live）；shadow 立即收敛为 CONFIRMED；
    - FAILED_DISPATCHED：外部明确返回失败（可补偿）；
    - FAILED_UNCERTAIN：提交结果不明（超时/中断），需对账或转人工；
    - COMPENSATING：正在补偿（回滚）；
    - RECONCILING：对账任务处理中。
    """

    PENDING_SUBMIT = "pending_submit"
    SUBMITTED = "submitted"
    CONFIRMED = "confirmed"
    FAILED_DISPATCHED = "failed_dispatch"
    FAILED_UNCERTAIN = "failed_uncertain"
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"
    COMPENSATION_FAILED = "compensation_failed"
    RECONCILING = "reconciling"
    RECONCILED = "reconciled"
    MISMATCHED = "mismatched"
    HUMAN_HANDOFF = "human_handoff"


# 合法跃迁表：{当前态: {允许的下一态}}
# 中间态都可异常收敛到 MISMATCHED / HUMAN_HANDOFF（转人工对账）；终态封闭不变。
_INTERMEDIATE_ABNORMAL = {
    ExecutionStatus.MISMATCHED.value,
    ExecutionStatus.HUMAN_HANDOFF.value,
}
_EXECUTION_TRANSITIONS: dict[str, set[str]] = {
    ExecutionStatus.PENDING_SUBMIT.value: {
        ExecutionStatus.SUBMITTED.value,
        ExecutionStatus.CONFIRMED.value,          # shadow 立即确认
        ExecutionStatus.FAILED_DISPATCHED.value,
        ExecutionStatus.FAILED_UNCERTAIN.value,
        *_INTERMEDIATE_ABNORMAL,
    },
    ExecutionStatus.SUBMITTED.value: {
        ExecutionStatus.CONFIRMED.value,          # 回调成功
        ExecutionStatus.FAILED_DISPATCHED.value,  # 回调失败
        ExecutionStatus.FAILED_UNCERTAIN.value,   # 超时/未知
        ExecutionStatus.COMPENSATING.value,       # 需补偿
        ExecutionStatus.RECONCILING.value,
        *_INTERMEDIATE_ABNORMAL,
    },
    ExecutionStatus.FAILED_DISPATCHED.value: {
        ExecutionStatus.COMPENSATING.value,
        ExecutionStatus.COMPENSATED.value,
        ExecutionStatus.COMPENSATION_FAILED.value,
        *_INTERMEDIATE_ABNORMAL,
    },
    ExecutionStatus.FAILED_UNCERTAIN.value: {
        ExecutionStatus.CONFIRMED.value,          # 对账发现其实成功
        ExecutionStatus.FAILED_DISPATCHED.value,  # 对账发现失败
        ExecutionStatus.RECONCILING.value,
        *_INTERMEDIATE_ABNORMAL,
    },
    ExecutionStatus.COMPENSATING.value: {
        ExecutionStatus.COMPENSATED.value,
        ExecutionStatus.COMPENSATION_FAILED.value,
        *_INTERMEDIATE_ABNORMAL,
    },
    ExecutionStatus.RECONCILING.value: {
        ExecutionStatus.CONFIRMED.value,
        ExecutionStatus.COMPENSATED.value,
        ExecutionStatus.MISMATCHED.value,
        ExecutionStatus.HUMAN_HANDOFF.value,
    },
    ExecutionStatus.CONFIRMED.value: set(),        # 终态
    ExecutionStatus.COMPENSATED.value: set(),      # 终态
    ExecutionStatus.COMPENSATION_FAILED.value: set(),  # 终态（转人工对账）
    ExecutionStatus.RECONCILED.value: set(),       # 终态
    ExecutionStatus.MISMATCHED.value: set(),       # 终态（转人工）
    ExecutionStatus.HUMAN_HANDOFF.value: set(),    # 终态
}


def can_transition(current: ExecutionStatus, next_status: ExecutionStatus) -> bool:
    """校验执行状态机是否允许从 current 跃迁到 next_status。

    终态（CONFIRMED/COMPENSATED/COMPENSATION_FAILED/RECONCILED/MISMATCHED/HUMAN_HANDOFF）
    一旦到达即封闭，任何再次跃迁都被拒绝（防重复扣款/退款）。
    """
    if current == next_status:
        return False
    return next_status.value in _EXECUTION_TRANSITIONS.get(current.value, set())


@dataclass(frozen=True)
class ExecutionRecord:
    """一笔业务执行的持久化事实（以 operation_id 为幂等锚点）。

    由 store 物化返回；外部只读。所有字段带租户作用域。
    """

    execution_id: str
    tenant_id: str
    operation_id: str
    pending_action: PendingAction
    order_id: str
    idempotency_key: str
    mode: ExecutionMode
    status: ExecutionStatus
    created_at: float
    updated_at: float
    amount: Optional[float] = None
    external_txn_id: Optional[str] = None
    callback_nonce: Optional[str] = None
    submitted_at: Optional[float] = None
    confirmed_at: Optional[float] = None
    receipt: Optional[dict] = None
    compensation_status: Optional[str] = None
    compensation_result: Optional[dict] = None
    last_error: Optional[str] = None
    attempts: int = 0

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            ExecutionStatus.CONFIRMED,
            ExecutionStatus.COMPENSATED,
            ExecutionStatus.COMPENSATION_FAILED,
            ExecutionStatus.RECONCILED,
            ExecutionStatus.MISMATCHED,
            ExecutionStatus.HUMAN_HANDOFF,
        }


@dataclass(frozen=True)
class ExecutionOutcome:
    """执行节点返回给图/调用方的最小必要结果（不含全部记录字段）。"""

    execution_id: str
    operation_id: str
    status: ExecutionStatus
    mode: ExecutionMode
    operation_status: OperationStatus   # 执行后 operation 应置的状态
    message: str
    receipt: Optional[dict] = None
    external_txn_id: Optional[str] = None
    human_handoff: bool = False
    error_code: Optional[str] = None


@dataclass(frozen=True)
class CallbackResult:
    """回调处理结果（供 webhook 端点与测试断言）。"""

    applied: bool
    reason: str
    execution_id: Optional[str] = None
    status: Optional[str] = None
    receipt: Optional[dict] = None


@dataclass(frozen=True)
class CallbackAtomicOutcome:
    """`store.apply_callback_atomic` 的原子回调结果（三后端一致，供 engine/routes 判断）。

    reason 取值约定（与 CallbackResult.reason 对齐）：
    - signature_invalid / bad_payload：由 engine 层负责（不触 DB，不在本结构出现）；
    - not_found / replay / amount_mismatch / illegal_transition / processing /
      confirmed / failed_dispatch / terminal_locked：由 store 原子方法判定并返回。
    其中 terminal_locked 表示执行记录已是终态（confirmed/compensated/mismatched/...），
    任何回调都不得覆盖；replay 表示与已记账 nonce 完全一致的重投（只重放不重复生效）。
    """

    applied: bool
    reason: str
    execution_id: Optional[str] = None
    status: Optional[str] = None
    receipt: Optional[dict] = None
    # 本次记账/重放所涉及的 nonce（供非重放窗口记账审计；None 表示未记账，如 not_found/终态无 nonce）。
    claimed_nonce: Optional[str] = None


@dataclass(frozen=True)
class ReconciliationResult:
    """对账任务的汇总结果（供后台任务与测试断言）。"""

    tenant_id: str
    scanned: int
    reconciled: int        # 对账后收敛为终态（confirmed/failed）
    mis_matched: int       # 对账不一致 → 转人工
    human_handoff: int     # 对账后转人工
    details: list[dict] = field(default_factory=list)
