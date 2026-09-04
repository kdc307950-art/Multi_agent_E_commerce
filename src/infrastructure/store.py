"""可插拔存储层。

当前实现：
- MemoryStore：默认（本地开发/单元测试），真实分布/持久化能力有限。
规划实现（待 Docker/预发布环境验证，不得宣称已可用）：
- SQLiteStore / PostgresStore：生产数据面；多租户隔离依赖应用层驻留守卫 +
  PostgreSQL RLS/等效 guard。RLS 必须在 saver 实际 SQL 的同一连接/事务内设置
  app.tenant_id（见《生产基线与验收测试》）。

所有读取/写入方法都以 tenant_id 作为强制参数，缺少有效租户作用域或跨租户访问
一律拒绝并写审计。任何方法都不允许"无租户条件"的查询。
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from src.core.types import (
    Approval,
    ApprovalStatus,
    AuditRecord,
    DomainError,
    ErrorCode,
    Membership,
    MembershipStatus,
    Operation,
    OperationStatus,
    PendingAction,
    Role,
    Session,
    SessionStatus,
    Tenant,
    TenantStatus,
)
from src.execution.types import (
    CallbackAtomicOutcome,
    ExecutionMode,
    ExecutionRecord,
    ExecutionStatus,
    can_transition,
)
from src.execution.verification import normalize_callback_payload

@dataclass
class _OperationRecord:
    rec: dict
    __slots__ = ("rec",)


def plan_callback_atomic(record: ExecutionRecord, *, nonce: str, payload: dict,
                         now: float) -> dict:
    """纯函数：根据锁内读取的执行记录与回调载荷，计算原子回调的应用计划（不触 DB）。

    三后端（Memory/Sqlite/Postgres）的 `apply_callback_atomic` 用同一计划在同一事务/临界区
    原子应用，保证「nonce 记账 + 状态 CAS + 操作更新 + 审计写入」不会中途分散提交产生状态不一致。

    reason 取值见 `CallbackAtomicOutcome`（不含 signature_invalid/bad_payload，属 engine 层）；
    若调用方查不到执行记录，由其自行返回 `not_found`（本函数只处理已定位到的记录）。
    返回计划 dict 字段：
      reason / applied / status / receipt / claimed_nonce / next_status / expected_status /
      book_nonce / operation_id / operation_status / operation_result / update_fields /
      audit_action / audit_detail
    """
    cb_amount = payload.get("amount")
    base_audit = {"nonce": nonce, "ext_status": payload.get("status"), "amount": cb_amount}

    def plan(*, reason: str, applied: bool, status: str | None = None,
             next_status: ExecutionStatus | None = None, receipt: dict | None = None,
             operation_status: OperationStatus | None = None,
             operation_result: dict | None = None, update_fields: dict | None = None,
             claimed_nonce: str | None = None) -> dict:
        # book_nonce：需要把本次 nonce 写入执行记录（首次记账 / 异 nonce 合法后续 / 新回调）。
        book_nonce = next_status is not None or reason == "processing"
        return {
            "reason": reason,
            "applied": applied,
            "status": status if status is not None else record.status.value,
            "next_status": next_status,
            "expected_status": record.status,
            "receipt": receipt,
            "claimed_nonce": claimed_nonce,
            "book_nonce": book_nonce,
            "operation_id": record.operation_id,
            "operation_status": operation_status,
            "operation_result": operation_result,
            "update_fields": update_fields or {},
            "audit_action": f"execution.callback.{reason}",
            "audit_detail": {"reason": reason, "applied": applied, **base_audit},
        }

    # 非重放窗口：与已记账 nonce 完全一致的重投 → 重放（无论是否终态，只重放不重复生效）。
    recorded_nonce = record.callback_nonce
    if recorded_nonce is not None and recorded_nonce == nonce:
        return plan(reason="replay", applied=False, claimed_nonce=recorded_nonce)
    # 终态封闭：异/新 nonce 落在终态记录上 → terminal_locked，绝不覆盖终态。
    if record.is_terminal:
        return plan(reason="terminal_locked", applied=False, claimed_nonce=recorded_nonce)

    # 金额一致性：回调金额与执行记录不一致 → 冲突转人工对账（记账后落到 mismatch 终态收口）。
    if cb_amount is not None and record.amount is not None and float(cb_amount) != record.amount:
        return plan(reason="amount_mismatch", applied=False,
                    next_status=ExecutionStatus.MISMATCHED,
                    status=ExecutionStatus.MISMATCHED.value,
                    operation_status=OperationStatus.HUMAN_HANDOFF,
                    operation_result={"execution_status": ExecutionStatus.MISMATCHED.value,
                                      "message": "回调金额与执行记录不一致，已转人工对账。"},
                    update_fields={"last_error": "amount_mismatch", "confirmed_at": now},
                    claimed_nonce=nonce)

    mapping = normalize_callback_payload(record.status.value, payload)
    next_status = ExecutionStatus(mapping["next"])
    if next_status == record.status:
        # 中间态（processing/unknown）：保持现状，仅记账（计入对账窗口，等待最终回调）。
        return plan(reason="processing", applied=False, claimed_nonce=nonce)

    if not can_transition(record.status, next_status):
        # 状态跃迁非法：几乎总是「该执行已被另一回调/补偿流程推进」（如失败回调先到的
        # FAILED_DISPATCHED、或已确认的 CONFIRMED）。此处**绝不覆写/改写既有流程**——否则会
        # 干扰补偿/确认的干净收口、并残留 last_error。一律按并发竞争/终态封闭拒绝
        # （terminal_locked，applied=False，不回执、不记账、不写审计、不改状态），
        # 让首个合法跃迁的一方继续收口到唯一确定终态。异常/需人工对账的场景由对账任务与
        # 金额一致性分支（amount_mismatch，发生在 SUBMITTED 才是业务确凿）各自处理。
        return plan(reason="terminal_locked", applied=False,
                    claimed_nonce=recorded_nonce or nonce)

    confirmed_at = now
    if next_status is ExecutionStatus.CONFIRMED:
        receipt = {"external_txn_id": payload.get("external_txn_id") or record.external_txn_id,
                   "order_id": record.order_id, "amount": record.amount or cb_amount,
                   "status": "succeeded", "provider_callback": True, "ts": confirmed_at}
        return plan(reason="confirmed", applied=True, next_status=next_status,
                    status=ExecutionStatus.CONFIRMED.value, receipt=receipt,
                    operation_status=OperationStatus.EXECUTED,
                    operation_result={"execution_status": ExecutionStatus.CONFIRMED.value,
                                      "mode": record.mode.value, "receipt": receipt,
                                      "message": "执行已确认（外部回调验签通过）。"},
                    update_fields={"confirmed_at": confirmed_at, "receipt": receipt},
                    claimed_nonce=nonce)

    # 外部失败回调：先原子收敛到 failed_dispatch（非终态）；补偿/对账决策在引擎层（provider 依赖）。
    return plan(reason="failed_dispatch", applied=True, next_status=next_status,
                status=ExecutionStatus.FAILED_DISPATCHED.value,
                operation_status=OperationStatus.EXECUTED,
                operation_result={"execution_status": ExecutionStatus.FAILED_DISPATCHED.value,
                                  "mode": record.mode.value,
                                  "message": "外部回调失败，执行记录已置为 failed_dispatch，待补偿/对账收口。"},
                update_fields={"confirmed_at": confirmed_at},
                claimed_nonce=nonce)


class MemoryStore:
    """内存仓储。线程/协程安全由单进程内单事件循环保证；适合开发与单元测试。

    RLS 说明：内存版没有数据库 RLS，改用"应用层租户作用域强制"——每个方法都要求
    显式 tenant_id，并对记录做归属校验，等价于 guards。PostgreSQL RLS 是生产目标，
    见《生产基线与验收测试》§四，本地不做宣称。
    """

    def __init__(self) -> None:
        self._tenants: dict[str, dict] = {}
        self._memberships: dict[tuple[str, str], dict] = {}
        self._sessions: dict[str, dict] = {}       # key: (tenant_id, thread_id)
        self._operations: dict[str, dict] = {}     # key: operation_id
        self._op_by_idem: dict[tuple[str, str], str] = {}  # (tenant_id, idempotency_key) -> op_id
        self._approvals: dict[str, dict] = {}      # key: approval_id
        self._executions: dict[str, dict] = {}     # key: execution_id
        self._exec_by_op: dict[tuple[str, str], str] = {}  # (tenant_id, operation_id) -> execution_id
        self._streams: dict[str, dict] = {}        # key: stream_id
        self._stream_events: dict[str, list[dict]] = {}  # stream_id -> [event]
        self._audit: list[dict] = []
        # 审批决策 CAS：单进程内线程锁，保证"读 pending -> 写终态"原子，防并发双执行。
        self._decision_lock = threading.Lock()

    # ---- 租户 / 成员 ----
    def create_tenant(self, tenant_id: str, name: str, status: TenantStatus = TenantStatus.ACTIVE) -> Tenant:
        if tenant_id in self._tenants:
            raise DomainError(ErrorCode.FORBIDDEN, "租户已存在", 409)
        self._tenants[tenant_id] = {
            "id": tenant_id, "name": name, "status": status.value,
            "created_at": time.time(),
        }
        return self.get_tenant(tenant_id)

    def get_tenant(self, tenant_id: str) -> Tenant:
        rec = self._tenants.get(tenant_id)
        if rec is None:
            raise DomainError(ErrorCode.NOT_FOUND, "租户不存在", 404)
        return Tenant(id=rec["id"], name=rec["name"], status=TenantStatus(rec["status"]), created_at=rec["created_at"])

    def require_active_tenant(self, tenant_id: str) -> Tenant:
        tenant = self.get_tenant(tenant_id)
        if tenant.status != TenantStatus.ACTIVE:
            raise DomainError(ErrorCode.TENANT_SUSPENDED, "租户已停用", 403)
        return tenant

    def set_tenant_status(self, tenant_id: str, status: TenantStatus) -> None:
        if tenant_id not in self._tenants:
            raise DomainError(ErrorCode.NOT_FOUND, "租户不存在", 404)
        self._tenants[tenant_id]["status"] = status.value

    def add_membership(self, tenant_id: str, user_id: str, role: Role) -> Membership:
        if role not in Role.tenant_roles():
            raise DomainError(ErrorCode.FORBIDDEN, "非法租户角色", 400)
        self.require_active_tenant(tenant_id)
        self._memberships[(tenant_id, user_id)] = {
            "tenant_id": tenant_id, "user_id": user_id, "role": role.value,
            "status": MembershipStatus.ACTIVE.value,
        }
        return self.get_membership(tenant_id, user_id)

    def get_membership(self, tenant_id: str, user_id: str) -> Optional[Membership]:
        rec = self._memberships.get((tenant_id, user_id))
        if rec is None:
            return None
        return Membership(tenant_id=rec["tenant_id"], user_id=rec["user_id"],
                          role=Role(rec["role"]), status=MembershipStatus(rec["status"]))

    def require_active_membership(self, tenant_id: str, user_id: str) -> Membership:
        self.require_active_tenant(tenant_id)
        m = self.get_membership(tenant_id, user_id)
        if m is None or m.status != MembershipStatus.ACTIVE:
            raise DomainError(ErrorCode.FORBIDDEN, "成员不存在或已撤销", 403)
        return m

    def revoke_membership(self, tenant_id: str, user_id: str) -> None:
        key = (tenant_id, user_id)
        if key not in self._memberships:
            raise DomainError(ErrorCode.NOT_FOUND, "成员不存在", 404)
        self._memberships[key]["status"] = MembershipStatus.REVOKED.value

    def list_members(self, tenant_id: str) -> list[Membership]:
        """列出租户内全部成员（租户作用域，用于 admin 成员管理视图）。"""
        result = []
        for (t, _), rec in self._memberships.items():
            if t != tenant_id:
                continue
            result.append(Membership(tenant_id=rec["tenant_id"], user_id=rec["user_id"],
                                     role=Role(rec["role"]), status=MembershipStatus(rec["status"])))
        return result

    # ---- 会话 ----
    def create_session(self, tenant_id: str, user_id: str, thread_id: str,
                       now: float, ttl_days: int) -> Session:
        self.require_active_membership(tenant_id, user_id)
        key = (tenant_id, thread_id)
        if key in self._sessions:
            raise DomainError(ErrorCode.FORBIDDEN, "会话已存在", 409)
        self._sessions[key] = {
            "thread_id": thread_id, "tenant_id": tenant_id, "user_id": user_id,
            "created_at": now, "expires_at": now + ttl_days * 86400,
            "last_active_at": now, "status": SessionStatus.ACTIVE.value,
            "message_count": 0, "title": None,
        }
        return self.get_session(tenant_id, thread_id)

    def get_session(self, tenant_id: str, thread_id: str) -> Session:
        rec = self._sessions.get((tenant_id, thread_id))
        if rec is None:
            raise DomainError(ErrorCode.NOT_FOUND, "会话不存在", 404)
        return Session(thread_id=rec["thread_id"], tenant_id=rec["tenant_id"],
                       user_id=rec["user_id"], created_at=rec["created_at"],
                       expires_at=rec["expires_at"], last_active_at=rec["last_active_at"],
                       status=SessionStatus(rec["status"]), message_count=rec["message_count"],
                       title=rec["title"])

    def get_session_or_none(self, tenant_id: str, thread_id: str) -> Optional[Session]:
        try:
            return self.get_session(tenant_id, thread_id)
        except DomainError:
            return None

    def touch_session(self, tenant_id: str, thread_id: str, now: float, ttl_days: int) -> None:
        rec = self._sessions.get((tenant_id, thread_id))
        if rec is None:
            raise DomainError(ErrorCode.NOT_FOUND, "会话不存在", 404)
        rec["last_active_at"] = now
        rec["expires_at"] = now + ttl_days * 86400

    def increment_message_count(self, tenant_id: str, thread_id: str, delta: int = 1) -> None:
        rec = self._sessions.get((tenant_id, thread_id))
        if rec is not None:
            rec["message_count"] += delta

    def list_sessions(self, tenant_id: str, user_id: Optional[str] = None) -> list[Session]:
        result = []
        for (t, _), rec in self._sessions.items():
            if t != tenant_id:
                continue
            if user_id is not None and rec["user_id"] != user_id:
                continue
            result.append(Session(thread_id=rec["thread_id"], tenant_id=rec["tenant_id"],
                                  user_id=rec["user_id"], created_at=rec["created_at"],
                                  expires_at=rec["expires_at"], last_active_at=rec["last_active_at"],
                                  status=SessionStatus(rec["status"]), message_count=rec["message_count"],
                                  title=rec["title"]))
        return result

    def mark_session_status(self, tenant_id: str, thread_id: str, status: SessionStatus) -> None:
        rec = self._sessions.get((tenant_id, thread_id))
        if rec is not None:
            rec["status"] = status.value

    # ---- 业务操作（幂等）----
    def create_operation(self, tenant_id: str, thread_id: str, order_id: str,
                         action: PendingAction, idempotency_key: str,
                         now: float) -> Operation:
        """幂等创建：同租户同 idempotency_key 已存在则返回既有操作（重放），不重复执行。"""
        existing = self._op_by_idem.get((tenant_id, idempotency_key))
        if existing is not None:
            return self.get_operation(tenant_id, existing)
        op_id = str(uuid.uuid4())
        self._operations[op_id] = {
            "operation_id": op_id, "tenant_id": tenant_id, "thread_id": thread_id,
            "order_id": order_id, "pending_action": action.value,
            "idempotency_key": idempotency_key, "status": OperationStatus.PENDING.value,
            "created_at": now, "result": None,
        }
        self._op_by_idem[(tenant_id, idempotency_key)] = op_id
        return self.get_operation(tenant_id, op_id)

    def get_operation(self, tenant_id: str, operation_id: str) -> Operation:
        rec = self._operations.get(operation_id)
        if rec is None or rec["tenant_id"] != tenant_id:
            raise DomainError(ErrorCode.NOT_FOUND, "操作不存在", 404)
        return Operation(operation_id=rec["operation_id"], tenant_id=rec["tenant_id"],
                         thread_id=rec["thread_id"], order_id=rec["order_id"],
                         pending_action=PendingAction(rec["pending_action"]),
                         idempotency_key=rec["idempotency_key"],
                         status=OperationStatus(rec["status"]), created_at=rec["created_at"],
                         result=rec["result"])

    def update_operation(self, tenant_id: str, operation_id: str, status: OperationStatus,
                         result: Optional[dict] = None) -> Operation:
        rec = self._operations.get(operation_id)
        if rec is None or rec["tenant_id"] != tenant_id:
            raise DomainError(ErrorCode.NOT_FOUND, "操作不存在", 404)
        rec["status"] = status.value
        if result is not None:
            rec["result"] = result
        return self.get_operation(tenant_id, operation_id)

    def list_operations(self, tenant_id: str, status: Optional[OperationStatus] = None) -> list[Operation]:
        result = []
        for rec in self._operations.values():
            if rec["tenant_id"] != tenant_id:
                continue
            if status is not None and rec["status"] != status.value:
                continue
            result.append(Operation(operation_id=rec["operation_id"], tenant_id=rec["tenant_id"],
                                    thread_id=rec["thread_id"], order_id=rec["order_id"],
                                    pending_action=PendingAction(rec["pending_action"]),
                                    idempotency_key=rec["idempotency_key"],
                                    status=OperationStatus(rec["status"]), created_at=rec["created_at"],
                                    result=rec["result"]))
        return result

    # ---- 审批 ----
    def create_approval(self, tenant_id: str, thread_id: str, operation_id: str,
                        action: PendingAction, order_id: Optional[str], amount: Optional[float],
                        reason: Optional[str], now: float) -> Approval:
        approval_id = str(uuid.uuid4())
        self._approvals[approval_id] = {
            "approval_id": approval_id, "tenant_id": tenant_id, "thread_id": thread_id,
            "operation_id": operation_id, "pending_action": action.value,
            "status": ApprovalStatus.PENDING.value, "created_at": now,
            "order_id": order_id, "amount": amount, "reason": reason,
            "approver": None, "feedback": None, "decided_at": None,
        }
        return self.get_approval(tenant_id, approval_id)

    def get_approval(self, tenant_id: str, approval_id: str) -> Approval:
        rec = self._approvals.get(approval_id)
        if rec is None or rec["tenant_id"] != tenant_id:
            raise DomainError(ErrorCode.NOT_FOUND, "审批单不存在", 404)
        return Approval(approval_id=rec["approval_id"], tenant_id=rec["tenant_id"],
                        thread_id=rec["thread_id"], operation_id=rec["operation_id"],
                        pending_action=PendingAction(rec["pending_action"]),
                        status=ApprovalStatus(rec["status"]), created_at=rec["created_at"],
                        order_id=rec["order_id"], amount=rec["amount"], reason=rec["reason"],
                        approver=rec["approver"], feedback=rec["feedback"], decided_at=rec["decided_at"])

    def list_approvals(self, tenant_id: str, status: Optional[ApprovalStatus] = None) -> list[Approval]:
        result = []
        for rec in self._approvals.values():
            if rec["tenant_id"] != tenant_id:
                continue
            if status is not None and rec["status"] != status.value:
                continue
            result.append(Approval(approval_id=rec["approval_id"], tenant_id=rec["tenant_id"],
                                   thread_id=rec["thread_id"], operation_id=rec["operation_id"],
                                   pending_action=PendingAction(rec["pending_action"]),
                                   status=ApprovalStatus(rec["status"]), created_at=rec["created_at"],
                                   order_id=rec["order_id"], amount=rec["amount"], reason=rec["reason"],
                                   approver=rec["approver"], feedback=rec["feedback"], decided_at=rec["decided_at"]))
        return result

    def decide_approval(self, tenant_id: str, approval_id: str, approver: str,
                        approved: bool, feedback: Optional[str], now: float) -> Approval:
        """审批决策（幂等重放）。内部走 CAS 抢占；已决策则返回既有结果，不重复生效。"""
        approval, _claimed = self.claim_approval_decision(
            tenant_id, approval_id, approver, approved, feedback, now)
        return approval

    def claim_approval_decision(self, tenant_id: str, approval_id: str, approver: str,
                                approved: bool, feedback: Optional[str],
                                now: float) -> tuple[Approval, bool]:
        """CAS 原子抢占 pending -> 终态（approved/rejected）。

        返回 (Approval, claimed)：claimed=True 表示本次调用真正把 pending 抢占为终态；
        claimed=False 表示已被其他路径决策（并发/重放），调用方不得恢复图执行，避免双执行。
        拒绝时同步把 operation 置为 rejected（或反馈要求转人工时 human_handoff）。
        """
        with self._decision_lock:
            rec = self._approvals.get(approval_id)
            if rec is None or rec["tenant_id"] != tenant_id:
                raise DomainError(ErrorCode.NOT_FOUND, "审批单不存在", 404)
            if rec["status"] != ApprovalStatus.PENDING.value:
                # 已决策：重放，不覆盖（不可抵赖），claimed=False。
                return self.get_approval(tenant_id, approval_id), False
            new_status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
            rec["status"] = new_status.value
            rec["approver"] = approver
            rec["feedback"] = feedback
            rec["decided_at"] = now
            self._apply_approval_decision_to_operation(
                tenant_id, rec["operation_id"], approved, feedback, now)
            return self.get_approval(tenant_id, approval_id), True

    def _apply_approval_decision_to_operation(self, tenant_id: str, operation_id: str,
                                              approved: bool, feedback: Optional[str],
                                              now: float) -> None:
        """审批终态联动 operation 状态：通过 -> 保持 pending（等 execute）；拒绝 -> rejected/human_handoff。"""
        if approved:
            return  # 通过：operation 保持 pending，由 execute 节点置 executed；不在此抢先。
        op_rec = self._operations.get(operation_id)
        if op_rec is None or op_rec["tenant_id"] != tenant_id:
            return
        if op_rec["status"] in (OperationStatus.EXECUTED.value, OperationStatus.HUMAN_HANDOFF.value):
            return  # 已执行/转人工不接受回退。
        op_rec["status"] = (OperationStatus.HUMAN_HANDOFF.value
                            if self._feedback_requests_handoff(feedback)
                            else OperationStatus.REJECTED.value)

    @staticmethod
    def _feedback_requests_handoff(feedback: Optional[str]) -> bool:
        """拒绝反馈是否请求转人工（决定 operation 标记 rejected 还是 human_handoff）。"""
        if not feedback:
            return False
        return any(k in feedback for k in ("人工", "转人工", "人工处理", "human", "handoff"))

    def expire_stale_approvals(self, tenant_id: str, now: float, timeout_seconds: float) -> list[str]:
        """审批超时恢复：把超过 timeout_seconds 仍未决策的 pending 审批标记为 timeout。

        对应 operation 置为 human_handoff（保留 operation_id / 业务状态供人工接管），
        禁止自动放行或重建新 operation_id。返回本次过期的 approval_id 列表。
        """
        expired: list[str] = []
        with self._decision_lock:
            for approval_id, rec in self._approvals.items():
                if rec["tenant_id"] != tenant_id or rec["status"] != ApprovalStatus.PENDING.value:
                    continue
                if rec["created_at"] + timeout_seconds > now:
                    continue
                rec["status"] = ApprovalStatus.TIMEOUT.value
                rec["decided_at"] = now
                op_rec = self._operations.get(rec["operation_id"])
                if (op_rec is not None and op_rec["tenant_id"] == tenant_id
                        and op_rec["status"] != OperationStatus.EXECUTED.value):
                    op_rec["status"] = OperationStatus.HUMAN_HANDOFF.value
                expired.append(approval_id)
        return expired

    # ---- 执行记录（资金/业务执行面，幂等锚点 = operation_id）----
    def create_execution_record(self, tenant_id: str, *, operation_id: str,
                                pending_action: PendingAction, order_id: str,
                                idempotency_key: str, mode: ExecutionMode, amount: float | None,
                                now: float) -> ExecutionRecord:
        """幂等创建执行记录：同租户同 operation_id 已存在则返回既有（重放），不重复提交外部。

        原子性（单执行守卫的前置）：在 `_decision_lock` 内做 **check-then-insert**，保证同一
        (tenant_id, operation_id) 并发下**只产生一条 execution record**。若此处不原子，高并发下
        多个线程会各自看到"无既有记录"并创建**不同的** execution_id，随后 `claim_execution_submit`
        会对各自不同的记录解锁（都 attempts=0）→ 多个线程各自 `provider.submit`（对外部重复提交）。
        与 sqlite/postgres 版以 DB `UNIQUE(tenant_id, operation_id)` + IntegrityError 兜底的语义一致。
        """
        with self._decision_lock:
            existing = self._exec_by_op.get((tenant_id, operation_id))
            if existing is not None:
                return self.get_execution_record(tenant_id, existing)
            exec_id = str(uuid.uuid4())
            self._executions[exec_id] = {
                "execution_id": exec_id, "tenant_id": tenant_id, "operation_id": operation_id,
                "pending_action": pending_action.value, "order_id": order_id,
                "idempotency_key": idempotency_key, "mode": mode.value,
                "status": ExecutionStatus.PENDING_SUBMIT.value, "created_at": now, "updated_at": now,
                "amount": amount, "external_txn_id": None, "callback_nonce": None,
                "submitted_at": None, "confirmed_at": None, "receipt": None,
                "compensation_status": None, "compensation_result": None, "last_error": None,
                "attempts": 0,
            }
            self._exec_by_op[(tenant_id, operation_id)] = exec_id
            return self.get_execution_record(tenant_id, exec_id)

    def claim_execution_submit(self, tenant_id: str, execution_id: str, *, now: float | None = None) -> bool:
        """单执行守卫：**原子抢占**"本次对 live 提交外部"的权利，仅一个线程能成功。

        用途：同一 operation 并发 execute 时，`create_execution_record` 幂等返回同一记录，若不
        加守卫，多个线程会**各自**调用 `provider.submit`（对外部重复提交）并在状态跃迁时因读到
        已推进的 SUBMITTED 抛 `submitted->submitted` 竞态（引擎并发缺陷，见 t4 复现）。

        using `attempts` 0→1 作为单次认领标记（本表 attempts 仅在此用作"首次提交认领"，与重试
        计数无关）。未认领（attempts=0 且 status=pending_submit）→ 置 1 并返回 True（本线程为
        提交者）；已被其他线程认领 / 已推进 → 返回 False，调用方应幂等重放，**绝不重复提交外部**。
        只在 pending_submit（非终态）上认领，终态记录 return False（不破坏 terminal_locked）。
        """
        with self._decision_lock:
            rec = self._executions.get(execution_id)
            if rec is None or rec["tenant_id"] != tenant_id:
                raise DomainError(ErrorCode.NOT_FOUND, "执行记录不存在", 404)
            if rec["status"] != ExecutionStatus.PENDING_SUBMIT.value or rec["attempts"] != 0:
                return False
            rec["attempts"] = 1
            rec["updated_at"] = now if now is not None else time.time()
            return True

    def get_execution_record(self, tenant_id: str, execution_id: str) -> ExecutionRecord:
        rec = self._executions.get(execution_id)
        if rec is None or rec["tenant_id"] != tenant_id:
            raise DomainError(ErrorCode.NOT_FOUND, "执行记录不存在", 404)
        return self._to_execution_record(rec)

    def get_execution_by_operation(self, tenant_id: str, operation_id: str) -> ExecutionRecord | None:
        exec_id = self._exec_by_op.get((tenant_id, operation_id))
        if exec_id is None:
            return None
        return self.get_execution_record(tenant_id, exec_id)

    def list_execution_records(self, tenant_id: str,
                               status: ExecutionStatus | None = None) -> list[ExecutionRecord]:
        result = []
        for rec in self._executions.values():
            if rec["tenant_id"] != tenant_id:
                continue
            if status is not None and rec["status"] != status.value:
                continue
            result.append(self._to_execution_record(rec))
        return result

    def update_execution_record(self, tenant_id: str, execution_id: str,
                                **fields) -> ExecutionRecord:
        expected_status = fields.pop("expected_status", None)
        with self._decision_lock:
            rec = self._executions.get(execution_id)
            if rec is None or rec["tenant_id"] != tenant_id:
                raise DomainError(ErrorCode.NOT_FOUND, "执行记录不存在", 404)
            # optional CAS：若提供了期望的当前状态，且当前状态与之不符（已被并发推进），
            # 返回 None，由调用方按"已被推进"幂等重放/收敛，而非对同一个旧状态重复跃迁抛错
            # （消除 submitted->submitted 之类非法跃迁向调用方抛错的问题）。
            if expected_status is not None and rec["status"] != expected_status.value:
                return None
            for k, v in fields.items():
                if k in ("status", "mode") and isinstance(v, (ExecutionStatus, ExecutionMode)):
                    v = v.value
                rec[k] = v
            rec["updated_at"] = fields.get("updated_at", time.time())
            return self.get_execution_record(tenant_id, execution_id)

    def claim_callback(self, tenant_id: str, execution_id: str,
                       nonce: str) -> tuple[ExecutionRecord, bool]:
        """回调非重放窗口的记账（CAS）：首次回调记账，同 nonce 重放，异 nonce 记为新回调。

        返回 (record, is_replay)。is_replay=True 表示与已记账 nonce 完全相同的重投
        （不重复生效）；否则本次回调可应用（首次或一个新的不同 nonce；终态收敛由引擎保证）。
        """
        with self._decision_lock:
            rec = self._executions.get(execution_id)
            if rec is None or rec["tenant_id"] != tenant_id:
                raise DomainError(ErrorCode.NOT_FOUND, "执行记录不存在", 404)
            if rec["callback_nonce"] is None:
                rec["callback_nonce"] = nonce
                rec["updated_at"] = time.time()
                return self.get_execution_record(tenant_id, execution_id), False
            if rec["callback_nonce"] == nonce:
                return self.get_execution_record(tenant_id, execution_id), True
            rec["callback_nonce"] = nonce  # 新的不同 nonce（合法的后续/最终回调）
            rec["updated_at"] = time.time()
            return self.get_execution_record(tenant_id, execution_id), False

    def apply_callback_atomic(self, tenant_id: str, *, execution_id: str, nonce: str,
                              payload: dict, now: float) -> CallbackAtomicOutcome:
        """回调原子应用（单临界区）：nonce 记账 + 执行状态 CAS + 操作更新 + 审计写入。

        与 Postgres/Sqlite 语义一致：查不到记录/跨租户 → not_found（不泄露存在）；
        同 nonce → replay；终态 + 异/新 nonce → terminal_locked；金额不一致 → amount_mismatch
        转人工；非法跃迁 → illegal_transition 转人工；中间态 processing → 仅记账；
        成功 → confirmed；外部失败 → failed_dispatch（补偿/对账在引擎层）。
        内存版以单进程 `_decision_lock` 模拟单写者 CAS；审计使用执行记录的可信 tenant_id。
        """
        with self._decision_lock:
            rec = self._executions.get(execution_id)
            if rec is None or rec["tenant_id"] != tenant_id:
                return CallbackAtomicOutcome(applied=False, reason="not_found",
                                             execution_id=execution_id, claimed_nonce=None)
            record = self._to_execution_record(rec)
            plan = plan_callback_atomic(record, nonce=nonce, payload=payload, now=now)
            # 原子应用计划（同一临界区内完成全部写）。
            if plan["book_nonce"]:
                rec["callback_nonce"] = nonce
            if plan["next_status"] is not None:
                rec["status"] = plan["next_status"].value
            rec["updated_at"] = now
            for k, v in plan["update_fields"].items():
                rec[k] = v
            if plan["operation_status"] is not None:
                op_rec = self._operations.get(plan["operation_id"])
                if op_rec is not None and op_rec["tenant_id"] == tenant_id:
                    op_rec["status"] = plan["operation_status"].value
                    op_rec["result"] = plan["operation_result"]
            # 只在「实际状态跃迁」（next_status 非空）时落权威回调审计：使用执行记录的可信
            # tenant_id，绝不使用请求体不可信 tenant_id。重放/终态封闭/中间态（无状态跃迁）
            # 不产生额外回调审计（供调用方区分"仅一次"的可信审计链）。
            if plan["next_status"] is not None:
                self.append_audit(record.tenant_id, "callback", plan["audit_action"], "execution",
                                  execution_id, plan["audit_detail"], now)
            return CallbackAtomicOutcome(applied=plan["applied"], reason=plan["reason"],
                                         execution_id=execution_id, status=plan["status"],
                                         receipt=plan["receipt"],
                                         claimed_nonce=plan["claimed_nonce"])

    @staticmethod
    def _to_execution_record(rec: dict) -> ExecutionRecord:
        return ExecutionRecord(
            execution_id=rec["execution_id"], tenant_id=rec["tenant_id"],
            operation_id=rec["operation_id"], pending_action=PendingAction(rec["pending_action"]),
            order_id=rec["order_id"], idempotency_key=rec["idempotency_key"],
            mode=ExecutionMode(rec["mode"]), status=ExecutionStatus(rec["status"]),
            created_at=rec["created_at"], updated_at=rec["updated_at"],
            amount=rec["amount"], external_txn_id=rec["external_txn_id"],
            callback_nonce=rec["callback_nonce"], submitted_at=rec["submitted_at"],
            confirmed_at=rec["confirmed_at"], receipt=rec["receipt"],
            compensation_status=rec["compensation_status"],
            compensation_result=rec["compensation_result"], last_error=rec["last_error"],
            attempts=rec["attempts"],
        )

    # ---- SSE 流 ----
    def create_stream(self, stream_id: str, tenant_id: str, user_id: str, thread_id: str,
                      client_request_id: str, mode: str, now: float) -> None:
        if stream_id in self._streams:
            raise DomainError(ErrorCode.FORBIDDEN, "流已存在", 409)
        self._streams[stream_id] = {
            "stream_id": stream_id, "tenant_id": tenant_id, "user_id": user_id,
            "thread_id": thread_id, "client_request_id": client_request_id,
            "mode": mode, "created_at": now, "last_seq": 0,
            "expires_at": now + 7 * 86400,
        }
        self._stream_events[stream_id] = []

    def find_stream_by_client(self, tenant_id: str, user_id: str, client_request_id: str) -> Optional[str]:
        """start 原子去重：同租户同用户同 client_request_id 只允许一个流。"""
        for rec in self._streams.values():
            if (rec["tenant_id"] == tenant_id and rec["user_id"] == user_id
                    and rec["client_request_id"] == client_request_id):
                return rec["stream_id"]
        return None

    def get_stream(self, tenant_id: str, stream_id: str) -> dict:
        rec = self._streams.get(stream_id)
        if rec is None or rec["tenant_id"] != tenant_id:
            raise DomainError(ErrorCode.NOT_FOUND, "流不存在", 404)
        return rec

    def append_event(self, tenant_id: str, stream_id: str, event: str, data: dict,
                     now: float) -> int:
        rec = self.get_stream(tenant_id, stream_id)
        rec["last_seq"] += 1
        seq = rec["last_seq"]
        self._stream_events[stream_id].append({
            "stream_id": stream_id, "seq": seq, "event": event, "data": data, "created_at": now,
        })
        return seq

    def events_after(self, tenant_id: str, stream_id: str, last_seq: int) -> list[dict]:
        self.get_stream(tenant_id, stream_id)
        events = self._stream_events.get(stream_id, [])
        return [e for e in events if e["seq"] > last_seq]

    # ---- 审计 ----
    def append_audit(self, tenant_id: str, user_id: str, action: str, target_type: str,
                     target_id: str, detail: dict, now: float) -> AuditRecord:
        record = AuditRecord(audit_id=str(uuid.uuid4()), tenant_id=tenant_id, user_id=user_id,
                             action=action, target_type=target_type, target_id=target_id,
                             detail=detail, created_at=now)
        self._audit.append({
            "audit_id": record.audit_id, "tenant_id": tenant_id, "user_id": user_id,
            "action": action, "target_type": target_type, "target_id": target_id,
            "detail": detail, "created_at": now,
        })
        return record

    def list_audit(self, tenant_id: str) -> list[AuditRecord]:
        result = []
        for rec in self._audit:
            if rec["tenant_id"] != tenant_id:
                continue
            result.append(AuditRecord(audit_id=rec["audit_id"], tenant_id=rec["tenant_id"],
                                      user_id=rec["user_id"], action=rec["action"],
                                      target_type=rec["target_type"], target_id=rec["target_id"],
                                      detail=rec["detail"], created_at=rec["created_at"]))
        return result

    def search_audit(self, tenant_id: str, **filters) -> list[AuditRecord]:
        """租户内多维审计追溯：可按 thread/目标类型/目标 id/action/user 过滤。

        全部在租户作用域内；`thread_id` 会命中 detail 中的 thread_id 或 target_id。
        用于关键审计按"租户/会话/审批/operation_id"四维追溯。
        """
        return filter_audit(self.list_audit(tenant_id), **filters)


def filter_audit(records: list[AuditRecord], *, thread_id: str | None = None,
                 target_type: str | None = None, target_id: str | None = None,
                 action: str | None = None, user_id: str | None = None,
                 limit: int | None = None) -> list[AuditRecord]:
    """对租户内审计记录按多维条件过滤（纯函数，供 memory/sqlite/postgres 共用）。"""
    out: list[AuditRecord] = []
    for rec in records:
        if target_type and rec.target_type != target_type:
            continue
        if target_id and rec.target_id != target_id:
            continue
        if action and rec.action != action:
            continue
        if user_id and rec.user_id != user_id:
            continue
        if thread_id:
            detail_thread = rec.detail.get("thread_id") if isinstance(rec.detail, dict) else None
            if detail_thread != thread_id and rec.target_id != thread_id and rec.target_type != "session":
                continue
            if detail_thread != thread_id and rec.target_id != thread_id:
                continue
        out.append(rec)
    if limit is not None:
        out = out[:limit]
    return out


def build_store(settings) -> MemoryStore | SqliteStore:
    """按 settings 选择存储后端。

    - memory：默认，仅内存（本地开发/单元测试）。
    - sqlite：本地持久化（开发验证），见 sqlite_store.SqliteStore。
    - postgres：生产目标，待 Docker/预发布实现并验证 RLS；未经验证前不返回。
    """
    from src.infrastructure.sqlite_store import SqliteStore

    backend = settings.storage_backend
    if backend == "memory":
        return MemoryStore()
    if backend == "sqlite":
        return SqliteStore(settings.sqlite_path)
    if backend == "postgres":
        from src.infrastructure.postgres_store import PostgresStore
        return PostgresStore(settings.database_url)
    raise DomainError(ErrorCode.INTERNAL_ERROR, f"未知存储后端: {backend}", 500)
