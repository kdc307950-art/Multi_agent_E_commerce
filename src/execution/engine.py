"""执行引擎：把「审批后执行」落到资金/业务执行面（状态机 + shadow/live + 补偿 + 回调 + 对账）。

设计依据（《错误处理与回退机制》§3.2/3.6 / §5）：
- 幂等优先：execute 以 operation_id 为锚点，已存在执行记录即重放；同一操作绝不重复提交外部。
- 状态机单向封闭：见 can_transition；终态后任何跃迁被拒绝（防重复扣款/退款）。
- shadow：第一轮只生成待执行记录 + 模拟回执，不触真实资金；live 才调用 FundsProvider，
  且 live 必须显式提供 provider（受控执行开关）。
- 异常收敛：外部失败/不确定 → 补偿或转人工 + 对账任务收口，绝不让未知状态静默通过。

多租户：所有方法以 tenant_id 为强制作用域；缺失/跨租户一律 DomainError。
"""
from __future__ import annotations

import hashlib
import json
import time

from src.core.types import (
    DomainError,
    ErrorCode,
    Operation,
    OperationStatus,
    PendingAction,
)
from src.execution.provider import FundsProvider, ProviderError
from src.execution.types import (
    CallbackResult,
    ExecutionMode,
    ExecutionRecord,
    ExecutionStatus,
    ExecutionOutcome,
    ReconciliationResult,
    can_transition,
)
from src.execution.verification import verify_hmac_signature


def _metrics():
    """惰性获取指标收集器。

    顶层常量 import 会触发 engine→observability→llm→tools→adapter→engine 的包级循环导入，
    故延迟到首次调用时导入；运行时行为与顶层 `from src.observability.metrics import get_metrics`
    完全一致（所有调用点都在方法体内，首次调用发生在模块完成加载之后）。
    """
    from src.observability.metrics import get_metrics
    return get_metrics()


def _reconcile_kind(reason: str) -> str:
    """把对账 mismatch 的细粒度 reason 归一为有界 kind（`query_failed:CODE` → `query_failed`）。

    reason 来自代码内固定集合（missing_external_txn_id / provider_not_configured /
    query_failed / already_terminal_confirmed / unknown_provider_status ...），
    归一后作为有界标签值，绝不携带订单/租户等明细。"""
    if not reason:
        return "unknown"
    base = reason.split(":", 1)[0].strip()
    return (base[:64]) if base else "unknown"


def _derive_external_txn_id(idempotency_key: str) -> str:
    """由业务幂等键派生稳定外部队列号（同幂等键同号，重放不重复）。"""
    return "txn-" + hashlib.sha1(idempotency_key.encode("utf-8")).hexdigest()[:16]


def _now() -> float:
    return time.time()


class ExecutionEngine:
    """执行引擎。mode 决定默认发起模式；可通过 execute(override_mode=...) 单次覆盖。

    provider 非 None 时用于 live 提交通道与对账查询；shadow 模式不需要 provider。
    """

    def __init__(self, store, *, mode: ExecutionMode = ExecutionMode.SHADOW,
                 provider: FundsProvider | None = None, callback_secret: str = "",
                 confirm_timeout_seconds: float = 900.0) -> None:
        self.store = store
        self.mode = mode
        self.provider = provider
        self.callback_secret = callback_secret
        self.confirm_timeout_seconds = confirm_timeout_seconds

    # ------------------------------------------------------------------
    # 执行入口（图 execute_* 节点 / 后台补偿与对账调用）
    # ------------------------------------------------------------------
    def execute(self, operation: Operation, order, *, override_mode: ExecutionMode | None = None) -> ExecutionOutcome:
        """执行一笔审批通过的操作。`order` 为已按租户/归属校验的 OrderRecord。

        shadow：生成待执行记录 + 模拟回执，不触真实资金。
        live：显式 provider 提交流，成功/失败/不确定分别收敛。
        """
        tenant_id = operation.tenant_id
        mode = override_mode or self.mode

        # 幂等：已有执行记录 → 重放（不重复提交）。
        existing = self.store.get_execution_by_operation(tenant_id, operation.operation_id)
        if existing is not None:
            return self._replay_outcome(existing, operation)

        # 资金一致性护栏：退款金额必须以订单实付为准，且为正。
        amount = self._resolve_amount(operation, order)

        record = self.store.create_execution_record(
            tenant_id,
            operation_id=operation.operation_id,
            pending_action=operation.pending_action,
            order_id=operation.order_id,
            idempotency_key=operation.idempotency_key,
            mode=mode,
            amount=amount,
            now=_now(),
        )
        # 执行提交计数（有界标签: mode=shadow|live）；仅对新执行记录计数（重放幂等不重复计）。
        _metrics().counter("execution_submit_total", ("mode",), {"mode": mode.value})
        self.store.append_audit(tenant_id, operation.thread_id, "execution.create",
                                "execution", record.execution_id,
                                {"operation_id": operation.operation_id,
                                 "mode": mode.value, "amount": amount}, _now())

        if mode is ExecutionMode.SHADOW:
            outcome = self._run_shadow(record, operation)
        else:
            outcome = self._run_live(record, operation)
        # 执行结果收敛计数（有界标签: mode,status）。
        _metrics().counter("execution_outcome_total", ("mode", "status"),
                              {"mode": mode.value, "status": outcome.status.value})
        return outcome

    def _run_shadow(self, record: ExecutionRecord, operation: Operation) -> ExecutionOutcome:
        """shadow：不触真实资金，合成确定性的模拟回执并立即收敛为 confirmed。"""
        receipt = {
            "external_txn_id": _derive_external_txn_id(record.idempotency_key),
            "operation_id": operation.operation_id,
            "order_id": operation.order_id,
            "amount": record.amount,
            "status": "succeeded",
            "simulated": True,
            "mode": ExecutionMode.SHADOW.value,
            "ts": _now(),
        }
        record = self._transition(record, ExecutionStatus.CONFIRMED,
                                  external_txn_id=receipt["external_txn_id"],
                                  submitted_at=_now(), confirmed_at=_now(), receipt=receipt)
        op = self.store.update_operation(tenant_id=operation.tenant_id, operation_id=operation.operation_id,
                                         status=OperationStatus.EXECUTED,
                                         result={"execution_status": ExecutionStatus.CONFIRMED.value,
                                                 "mode": ExecutionMode.SHADOW.value,
                                                 "execution_id": record.execution_id,
                                                 "simulated": True,
                                                 "receipt": receipt,
                                                 "message": "退款/退货/改址已在 shadow 模式下生成模拟回执。"})
        return ExecutionOutcome(execution_id=record.execution_id, operation_id=operation.operation_id,
                                status=ExecutionStatus.CONFIRMED, mode=ExecutionMode.SHADOW,
                                operation_status=OperationStatus.EXECUTED, receipt=receipt,
                                external_txn_id=receipt["external_txn_id"],
                                message="执行完成（shadow 模拟回执，未调用真实资金接口）。")

    def _run_live(self, record: ExecutionRecord, operation: Operation) -> ExecutionOutcome:
        """live：显式 provider 提交；成功/失败/不确定分别收敛，绝不静默放行。

        单执行守卫：`claim_execution_submit` 原子抢占"本次提交外部"的权利（attempts 0→1），
        仅一个并行线程成为提交者；其余并行线程（读到同一 pending_submit 记录）被判为非提交者，
        直接幂等重放——**绝不重复调用 provider.submit、绝不进入 submitted->submitted 状态跃迁竞态**
        （消除"并发重复执行/重复补偿"隐患）。终态记录在 claim 时返回 False，保留 terminal_locked。
        """
        if self.provider is None:
            raise DomainError(ErrorCode.EXECUTION_FAILED,
                              "live 执行模式必须配置 FundsProvider（受控执行开关未开启）。", 503)
        # 单执行守卫：非提交者幂等重放，不重复提交外部。
        if not self.store.claim_execution_submit(record.tenant_id, record.execution_id):
            fresh = self.store.get_execution_record(record.tenant_id, record.execution_id)
            return self._replay_outcome(fresh, operation)
        try:
            result = self.provider.submit(
                tenant_id=operation.tenant_id, operation_id=operation.operation_id,
                pending_action=operation.pending_action.value, order_id=operation.order_id,
                amount=record.amount or 0.0, idempotency_key=record.idempotency_key,
            )
        except ProviderError as exc:
            # 提交结果不明（网络/超时/服务端错误）→ 置为不确定，交对账任务收口，不重复提交。
            record = self._transition(record, ExecutionStatus.FAILED_UNCERTAIN,
                                      last_error=f"{exc.code}: {exc.message}")
            op = self.store.update_operation(
                operation.tenant_id, operation.operation_id, OperationStatus.EXECUTED,
                result={"execution_status": ExecutionStatus.FAILED_UNCERTAIN.value,
                        "mode": ExecutionMode.LIVE.value,
                        "waiting_reconcile": True,
                        "message": "执行提交结果不明，已进入对账任务收口。"})
            self.store.append_audit(operation.tenant_id, operation.thread_id,
                                    "execution.submit_uncertain", "execution", record.execution_id,
                                    {"external_txn_id": record.external_txn_id,
                                     "last_error": exc.code}, _now())
            return ExecutionOutcome(execution_id=record.execution_id,
                                    operation_id=operation.operation_id,
                                    status=ExecutionStatus.FAILED_UNCERTAIN,
                                    mode=ExecutionMode.LIVE,
                                    operation_status=OperationStatus.EXECUTED,
                                    external_txn_id=record.external_txn_id,
                                    message="执行提交结果不明，正在对账，请稍候确认。")

        external_txn_id = result.get("external_txn_id") or _derive_external_txn_id(record.idempotency_key)
        provider_status = result.get("status")
        if provider_status in ("succeeded", "success"):
            # 已提交外部，等待最终回调确认（回调重放只重放不重复；对账兜底）。
            # expected_status=PENDING_SUBMIT：CAS 只在记录仍为 pending 时推进到 submitted；
            # 若已被其他线程/回调并发推进（如已 confirmed/compensated），返回现状记录 → 幂等重放，
            # 不抛 submitted->submitted 之类非法跃迁（red/yellow 点②）。
            record = self._transition(record, ExecutionStatus.SUBMITTED,
                                      expected_status=ExecutionStatus.PENDING_SUBMIT,
                                      external_txn_id=external_txn_id, submitted_at=_now(),
                                      receipt=result.get("receipt"))
            if record.status is not ExecutionStatus.SUBMITTED:
                # CAS 落空（已被并发推进到终态/其他态）→ 按现状幂等重放，不重复提交/不抛错。
                return self._replay_outcome(record, operation)
            op = self.store.update_operation(
                operation.tenant_id, operation.operation_id, OperationStatus.EXECUTED,
                result={"execution_status": ExecutionStatus.SUBMITTED.value,
                        "mode": ExecutionMode.LIVE.value,
                        "execution_id": record.execution_id,
                        "external_txn_id": external_txn_id,
                        "message": "执行已提交，等待资金回调确认。"})
            return ExecutionOutcome(execution_id=record.execution_id,
                                    operation_id=operation.operation_id,
                                    status=ExecutionStatus.SUBMITTED, mode=ExecutionMode.LIVE,
                                    operation_status=OperationStatus.EXECUTED,
                                    receipt=result.get("receipt"), external_txn_id=external_txn_id,
                                    message="执行已提交，等待回调确认。")
        # 外部明确失败 → 走失败补偿/转人工，绝不当作成功。
        record = self._transition(record, ExecutionStatus.FAILED_DISPATCHED,
                                  external_txn_id=external_txn_id, last_error=f"provider_status={provider_status}")
        return self._handle_dispatched_failure(record, operation)

    def _handle_dispatched_failure(self, record: ExecutionRecord, operation: Operation) -> ExecutionOutcome:
        """外部明确失败：尝试补偿（回滚），补偿失败 → 转人工对账。

        补偿以同一 idempotency_key + execution_id 发起，重放一致；失败收敛到 human_handoff。
        """
        record = self._settle_after_dispatch_failure(record, operation.tenant_id, operation.operation_id)
        if record.status is ExecutionStatus.COMPENSATED:
            return ExecutionOutcome(execution_id=record.execution_id,
                                    operation_id=operation.operation_id,
                                    status=ExecutionStatus.COMPENSATED, mode=record.mode,
                                    operation_status=OperationStatus.EXECUTED,
                                    receipt=record.compensation_result,
                                    message="外部执行失败，已补偿（回滚）。")
        # 执行失败且补偿失败 → 人工介入（kind=execution_handoff，有界标签）。
        _metrics().counter("human_intervention_total", ("kind",),
                              {"kind": "execution_handoff"})
        return ExecutionOutcome(execution_id=record.execution_id,
                                operation_id=operation.operation_id,
                                status=record.status, mode=record.mode,
                                operation_status=OperationStatus.HUMAN_HANDOFF,
                                human_handoff=True,
                                error_code=ErrorCode.EXECUTION_FAILED.value,
                                message="执行失败且补偿失败，已转人工对账。")

    def _settle_after_dispatch_failure(self, record: ExecutionRecord, tenant_id: str,
                                       operation_id: str) -> ExecutionRecord:
        """把一笔已 FAILED_DISPATCHED 的执行收口为 COMPENSATED 或 COMPENSATION_FAILED(HUMAN)。"""
        # 先把状态规整到 FAILED_DISPATCHED（若是 SUBMITTED/FAILED_UNCERTAIN 需先归一）。
        if record.status is not ExecutionStatus.FAILED_DISPATCHED:
            if can_transition(record.status, ExecutionStatus.FAILED_DISPATCHED):
                record = self._transition(record, ExecutionStatus.FAILED_DISPATCHED)
            else:
                record = self._transition(record, ExecutionStatus.HUMAN_HANDOFF,
                                          last_error="cannot_normalize_to_failed")
                self.store.update_operation(tenant_id, operation_id, OperationStatus.HUMAN_HANDOFF,
                                            result={"execution_status": ExecutionStatus.HUMAN_HANDOFF.value,
                                                    "message": "执行状态异常，已转人工对账。"})
                return record
        comp = self._compensate(record)
        if comp.get("status") == "succeeded":
            record = self._transition(record, ExecutionStatus.COMPENSATED,
                                      compensation_status="compensated",
                                      compensation_result=comp.get("receipt"), confirmed_at=_now())
            self.store.update_operation(tenant_id, operation_id, OperationStatus.EXECUTED,
                                        result={"execution_status": ExecutionStatus.COMPENSATED.value,
                                                "mode": record.mode.value, "compensated": True,
                                                "message": "外部执行失败，已补偿（回滚）。"})
            self.store.append_audit(
                tenant_id, "execution", "execution.compensated", "execution", record.execution_id,
                {"operation_id": operation_id, "reversal_id": (comp.get("receipt") or {}).get("reversal_id"),
                 "reason": "dispatch_failure_compensated"}, _now())
        else:
            record = self._transition(record, ExecutionStatus.COMPENSATION_FAILED,
                                      compensation_status="compensation_failed", confirmed_at=_now())
            self.store.update_operation(tenant_id, operation_id, OperationStatus.HUMAN_HANDOFF,
                                        result={"execution_status": ExecutionStatus.COMPENSATION_FAILED.value,
                                                "message": "执行失败且补偿失败，已转人工对账。"})
            self.store.append_audit(
                tenant_id, "execution", "execution.compensation_failed", "execution", record.execution_id,
                {"operation_id": operation_id, "reason": comp.get("reason", "compensation_failed"),
                 "kind": "human_handoff"}, _now())
        return record

    # ------------------------------------------------------------------
    # 补偿
    # ------------------------------------------------------------------
    def compensate(self, tenant_id: str, execution_id: str) -> dict:
        """对一笔执行发起补偿（回滚）。供后台补偿任务/对账调用。

        幂等：记录已处于终止性补偿态（COMPENSATED / COMPENSATION_FAILED）时**幂等重放**——
        返回既有补偿结果，不再发起外部反向、不再跃迁（终态封闭）。并发下多个线程对同一记录
        反复补偿也只产生一次外部反向/一次跃迁（其余按现状收敛/返回既有结果），绝不抛
        "compensated -> compensated" 之类非法跃迁给调用方。
        """
        record = self.store.get_execution_record(tenant_id, execution_id)
        if record is None:
            raise DomainError(ErrorCode.NOT_FOUND, "执行记录不存在", 404)
        # 终态封闭：已补偿/补偿失败 → 幂等重放既有结果（补偿记录含 reversal_id，重放一致）。
        if record.status in (ExecutionStatus.COMPENSATED, ExecutionStatus.COMPENSATION_FAILED):
            return {"status": "succeeded" if record.status is ExecutionStatus.COMPENSATED else "failed",
                    "receipt": record.compensation_result or {},
                    "replayed": True}
        result = self._compensate(record)
        if result.get("status") == "succeeded":
            self._transition(record, ExecutionStatus.COMPENSATED,
                             compensation_status="compensated",
                             compensation_result=result.get("receipt"), confirmed_at=_now())
            self.store.append_audit(
                tenant_id, "execution", "execution.compensated", "execution", execution_id,
                {"operation_id": record.operation_id,
                 "reversal_id": (result.get("receipt") or {}).get("reversal_id"),
                 "reason": "manual_compensate"}, _now())
        else:
            self._transition(record, ExecutionStatus.COMPENSATION_FAILED,
                             compensation_status="compensation_failed", confirmed_at=_now())
            self.store.append_audit(
                tenant_id, "execution", "execution.compensation_failed", "execution", execution_id,
                {"operation_id": record.operation_id, "reason": result.get("reason", "compensation_failed"),
                 "kind": "human_handoff"}, _now())
        return result

    def _compensate(self, record: ExecutionRecord) -> dict:
        """调用 provider.compensate 发起反向；shadow 或无从 provider 时合成确定性回执。"""
        if self.provider is not None:
            try:
                return self.provider.compensate(
                    tenant_id=record.tenant_id, execution_id=record.execution_id,
                    idempotency_key=record.idempotency_key, amount=record.amount or 0.0)
            except ProviderError as exc:
                return {"status": "failed", "reason": f"{exc.code}: {exc.message}"}
        return {"status": "succeeded",
                "receipt": {"reversal_id": "rev-" + _derive_external_txn_id(record.idempotency_key)[4:],
                            "execution_id": record.execution_id, "amount": record.amount,
                            "status": "reversed", "simulated": True}}

    # ------------------------------------------------------------------
    # 回调验签 + 重放保护
    # ------------------------------------------------------------------
    def apply_callback(self, raw_body: bytes, signature: str) -> CallbackResult:
        """外部资金网关回调：验签 → 定位执行记录 → 非重放窗口 → 状态跃迁（原子应用）。

        所有 DB 写（nonce 记账 + 状态 CAS 终态封闭 + 操作更新 + 审计写入）由
        `store.apply_callback_atomic` 在**同一事务**内完成，杜绝「记账成功但状态未跃迁」等
        中途失败产生的状态不一致。验签/载荷解析/标识提取在引擎层（不触 DB）。

        非重放约定：
        - 终态记录（confirmed/compensated/mismatched/human_handoff/...）封闭：任何异/新 nonce
          回调都被 terminal_locked 拒绝，绝不覆盖终态（防重复扣款/退款）；
        - 与已记账 nonce 完全相同的重投 → 重放（只重放不重复生效）；
        - 一个真实的新 nonce（合法的后续/最终回调）在终态前允许应用（first-wins 终态封闭）。
        """
        if not verify_hmac_signature(self.callback_secret, raw_body, signature):
            return CallbackResult(applied=False, reason="signature_invalid")
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception:
            return CallbackResult(applied=False, reason="bad_payload")
        tenant_id = str(payload.get("tenant_id") or "")
        execution_id = str(payload.get("execution_id") or "")
        if not tenant_id or not execution_id:
            return CallbackResult(applied=False, reason="missing_identity")
        nonce = str(payload.get("nonce") or "")
        if not nonce:
            return CallbackResult(applied=False, reason="missing_nonce", execution_id=execution_id)

        # 原子回调应用（含非重放窗口 + 终态封闭 + 操作/审计联动）。
        outcome = self.store.apply_callback_atomic(
            tenant_id, execution_id=execution_id, nonce=nonce, payload=payload, now=_now())

        if outcome.reason == "failed_dispatch":
            # 外部明确失败：原子方法已收敛到 failed_dispatch（非终态）；补偿/转人工决策依赖
            # provider，在引擎层收口（后台对账任务同路径幂等补偿，不会重复生效）。
            record = self.store.get_execution_record(tenant_id, execution_id)
            record = self._settle_after_dispatch_failure(
                record, record.tenant_id, record.operation_id)
            return CallbackResult(applied=True, reason="failed_dispatch", execution_id=execution_id,
                                  status=record.status.value, receipt=record.receipt)

        # not_found / terminal_locked / amount_mismatch / illegal_transition / processing /
        # confirmed / replay：直接暴露 store 原子方法判定（跨租户 → not_found 由路由层转 HTTP 404，
        # 终态封闭 → terminal_locked 供调用方区分）。
        return CallbackResult(applied=outcome.applied, reason=outcome.reason,
                              execution_id=execution_id, status=outcome.status,
                              receipt=outcome.receipt)

    # ------------------------------------------------------------------
    # 对账任务
    # ------------------------------------------------------------------
    def _is_overdue(self, record: ExecutionRecord, now: float | None = None) -> bool:
        """判断一笔已提交(submitted)执行是否超过确认超时窗口（confirm_timeout_seconds）。

        仅对「已提交但尚未确认」的 submitted 记录判定；submitted_at 缺失（提交时刻未知，
        见 D6 演练人为置为 submitted）不算 overdue——此类记录交由通用对账兜底，不在此误判。
        超时未确认 → true，供对账/转人工强制收口，绝不静默当成功。
        """
        if record.status is not ExecutionStatus.SUBMITTED:
            return False
        if record.submitted_at is None:
            return False
        now = now if now is not None else _now()
        return now - record.submitted_at > self.confirm_timeout_seconds

    def list_overdue_reconciliation_targets(self, tenant_id: str, limit: int = 200) -> list[ExecutionRecord]:
        """返回「已提交但超过 confirm_timeout_seconds 仍未确认」的 overdue 执行记录。

        该类记录是超时未确认的高风险项：必须强制进入对账（查询外部真实状态）或转人工，
        绝不允许停留在 submitted 被当作成功。所有查询以 tenant_id 强制作用域。
        """
        if not tenant_id:
            raise DomainError(ErrorCode.FORBIDDEN, "缺少租户作用域", 400)
        return [r for r in self.store.list_execution_records(tenant_id)
                if self._is_overdue(r)][:limit]

    def list_reconciliation_targets(self, tenant_id: str, limit: int = 200) -> list[ExecutionRecord]:
        """返回该租户需要对账的非终态/异常执行记录（submitted / failed_uncertain / reconciling）。

        依据 confirm_timeout_seconds 语义：把「提交后超过确认超时仍未确认」的 overdue 记录
        优先返回，确保超时未确认的执行一定进入对账/转人工（即使受 limit 截断也只可能丢弃
        尚未超时的记录，绝不丢弃 overdue）。其余非终态/异常记录维持在既有对账范围。
        """
        if not tenant_id:
            raise DomainError(ErrorCode.FORBIDDEN, "缺少租户作用域", 400)
        wanted = {ExecutionStatus.SUBMITTED.value, ExecutionStatus.FAILED_UNCERTAIN.value,
                  ExecutionStatus.RECONCILING.value, ExecutionStatus.FAILED_DISPATCHED.value}
        records = self.store.list_execution_records(tenant_id)
        overdue = [r for r in records if self._is_overdue(r)]
        others = [r for r in records if r.status.value in wanted and not self._is_overdue(r)]
        # overdue 优先：limit 截断时默认丢弃尚未超时记录，绝不让超时未确认记录被漏掉。
        return (overdue + others)[:limit]

    def reconcile(self, tenant_id: str) -> ReconciliationResult:
        """对账：查询外部真实状态并收口，杜绝「内部认为已提交/未知但实际已扣款/退款」。

        - 通用对账目标（overdue 优先）已接入 confirm_timeout_seconds；
        - 超时未确认(overdue)记录额外强制并入（不因 limit 漏掉），确保对这类记录执行
          query 外部核实；
        - 冲突或外部不可达/状态未知 → mismatch → 转人工；外部明确失败 → 补偿，
          补偿失败 → 转人工。保留完整执行记录与审计，绝不静默当成功。
        """
        targets, seen = [], set()
        for rec in self.list_reconciliation_targets(tenant_id):
            if rec.execution_id not in seen:
                seen.add(rec.execution_id)
                targets.append(rec)
        # 超时未确认记录强制并入：无论通用列表是否受 limit 影响，都进入对账/转人工。
        for rec in self.list_overdue_reconciliation_targets(tenant_id, limit=5000):
            if rec.execution_id not in seen:
                seen.add(rec.execution_id)
                targets.append(rec)
        scanned, reconciled, mis_matched, handoff = len(targets), 0, 0, 0
        details: list[dict] = []
        for record in targets:
            outcome, detail = self._reconcile_one(tenant_id, record)
            details.append({"execution_id": record.execution_id, "outcome": outcome})
            if outcome == "confirmed":
                reconciled += 1
            elif outcome == "mismatch":
                mis_matched += 1
            elif outcome == "human_handoff":
                handoff += 1
        # 对账最后运行时间（gauge，无标签；供「对账超时未运行」告警 time() - gauge > 86400 判定）。
        _metrics().set("reconcile_last_run_timestamp_seconds", _now(), (), {})
        return ReconciliationResult(tenant_id=tenant_id, scanned=scanned, reconciled=reconciled,
                                    mis_matched=mis_matched, human_handoff=handoff,
                                    details=details)

    def _reconcile_one(self, tenant_id: str, record: ExecutionRecord) -> tuple[str, str]:
        """对账单条记录：返回 (outcome, detail)。outcome ∈ {confirmed, mismatch, human_handoff}。

        对 overdue（提交后超过 confirm_timeout_seconds 仍未确认）记录在 detail 中显式标注，
        确保超时未确认可被追踪；无论是否 overdue，查询失败/外部状态未知一律走 mismatch 转人工，
        外部明确失败走补偿（补偿失败转人工），绝不静默当成功。
        """
        overdue = self._is_overdue(record)
        tag = "overdue_" if overdue else ""
        if record.external_txn_id is None:
            self._reconcile_mismatch(record, tag + "missing_external_txn_id")
            return "mismatch", tag + "missing_external_txn_id"
        if self.provider is None:
            self._reconcile_mismatch(record, tag + "provider_not_configured")
            return "mismatch", tag + "provider_not_configured"
        try:
            ext = self.provider.query(tenant_id=tenant_id, external_txn_id=record.external_txn_id)
        except ProviderError as exc:
            self._reconcile_mismatch(record, f"{tag}query_failed:{exc.code}")
            return "mismatch", f"{tag}query_failed:{exc.code}"
        ext_status = ext.get("status")
        if ext_status in ("succeeded", "success"):
            if not can_transition(record.status, ExecutionStatus.CONFIRMED):
                self._reconcile_mismatch(record, tag + "already_terminal_confirmed")
                return "mismatch", tag + "already_terminal_confirmed"
            self._transition(record, ExecutionStatus.CONFIRMED, confirmed_at=_now(),
                             receipt=ext.get("receipt") or record.receipt)
            self.store.update_operation(tenant_id, record.operation_id, OperationStatus.EXECUTED,
                                        result={"execution_status": ExecutionStatus.CONFIRMED.value,
                                                "reconciled": True,
                                                "message": "对账确认执行成功。"})
            self.store.append_audit(
                tenant_id, "execution", "execution.reconcile.confirmed", "execution",
                record.execution_id,
                {"operation_id": record.operation_id, "external_txn_id": record.external_txn_id,
                 "reason": ("overdue_confirmed_by_reconcile" if overdue
                            else "confirmed_by_reconcile")}, _now())
            return "confirmed", ("overdue_confirmed_by_reconcile" if overdue
                                 else "confirmed_by_reconcile")
        if ext_status in ("failed", "rejected"):
            self._reconcile_failure(record, "provider_failed")
            final = self.store.get_execution_record(tenant_id, record.execution_id)
            return ("human_handoff" if final.status is ExecutionStatus.COMPENSATION_FAILED
                    else "confirmed"), ("overdue_compensated_after_failure" if overdue
                                        else "compensated_after_failure")
        self._reconcile_mismatch(record, f"{tag}unknown_provider_status:{ext_status}")
        return "mismatch", f"{tag}unknown_provider_status:{ext_status}"

    def _reconcile_failure(self, record: ExecutionRecord, reason: str) -> None:
        """对账发现外部失败：先归一为 failed，发补偿（回滚）；补偿成功 → compensated，否则转人工。"""
        self._settle_after_dispatch_failure(record, record.tenant_id, record.operation_id)

    def _reconcile_mismatch(self, record: ExecutionRecord, reason: str) -> None:
        """对账冲突：标记 mismatch + operation 转人工，保留记录供人工对账。"""
        # 对账不一致计数（有界 kind）+ 人工介入计数（kind=reconcile_mismatch）。
        _metrics().counter("reconcile_mismatch_total", ("kind",),
                              {"kind": _reconcile_kind(reason)})
        _metrics().counter("human_intervention_total", ("kind",),
                              {"kind": "reconcile_mismatch"})
        self._transition(record, ExecutionStatus.MISMATCHED, last_error=reason, confirmed_at=_now())
        self.store.update_operation(record.tenant_id, record.operation_id, OperationStatus.HUMAN_HANDOFF,
                                    result={"execution_status": ExecutionStatus.MISMATCHED.value,
                                            "message": "对账不一致，已转人工对账。", "reason": reason})
        self.store.append_audit(
            record.tenant_id, "execution", "execution.reconcile.mismatch", "execution",
            record.execution_id,
            {"operation_id": record.operation_id, "reason": reason, "kind": "human_handoff"}, _now())

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _replay_outcome(self, record: ExecutionRecord, operation: Operation) -> ExecutionOutcome:
        """幂等重放：返回既有执行记录对应的结果（不重复提交 vs 外部）。"""
        return ExecutionOutcome(
            execution_id=record.execution_id, operation_id=operation.operation_id,
            status=record.status, mode=record.mode,
            operation_status=self.store.get_operation(operation.tenant_id, operation.operation_id).status,
            receipt=record.receipt, external_txn_id=record.external_txn_id,
            human_handoff=record.status in (ExecutionStatus.HUMAN_HANDOFF, ExecutionStatus.MISMATCHED),
            message="该操作已存在执行记录，返回既有结果（幂等重放，未重复执行）。")

    def _resolve_amount(self, operation: Operation, order) -> float | None:
        """退款金额 = 订单实付；退货/改址为 None。非法金额拒绝执行（fail-closed）。"""
        if operation.pending_action is PendingAction.REFUND:
            if order is None or order.total_amount <= 0:
                raise DomainError(ErrorCode.EXECUTION_FAILED, "退款金额非法，已转人工。", 422)
            return float(order.total_amount)
        return None

    def _transition(self, record: ExecutionRecord, next_status: ExecutionStatus,
                    expected_status: ExecutionStatus | None = None, **fields) -> ExecutionRecord:
        """按状态机合法跃迁；非法跃迁抛 DomainError（存疑 → 上层转人工）。

        `expected_status` 可选：当调用方对**并发推进**敏感（如 live 提交后等待回调）时显式传入，
        透传给 store 的 `update_execution_record(expected_status=...)`，由 store 基于**实际当前状态**
        做 CAS（`WHERE status=expected_status`）。CAS 落空（记录已被并发推进，如已被回调确认）→
        返回**当前记录**（调用方据此幂等重放），不抛 DomainError——消除高并发下
        "submitted -> submitted"之类向调用方抛非法跃迁的问题（red/yellow 点②）。
        无 expected_status 时保留 `can_transition` 校验（状态机核心，防非法跃迁/终态开放）。
        """
        if not can_transition(record.status, next_status):
            # 同状态/反向/终态重复跃迁一律拒绝（防绕过状态机）；除非是并发落空情形（见下）。
            # 说明：多线程对同一终态记录重复跃迁会在此抛错，属设计意图（终态封闭）；
            # 仅在显式传 expected_status 且记录已被并发推进时走 CAS 重放（绝不抛给用户）。
            if expected_status is None:
                raise DomainError(ErrorCode.EXECUTION_FAILED,
                                  f"执行状态跃迁非法: {record.status.value} -> {next_status.value}", 422)
        updated = self.store.update_execution_record(record.tenant_id, record.execution_id,
                                                     status=next_status, updated_at=_now(),
                                                     expected_status=expected_status, **fields)
        if updated is None:
            # CAS 落空：记录已被并发推进 → 读回当前状态按现状幂等收敛（不重复提交/不抛错）。
            return self.store.get_execution_record(record.tenant_id, record.execution_id)
        return updated
