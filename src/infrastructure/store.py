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
from src.execution.types import ExecutionMode, ExecutionRecord, ExecutionStatus

@dataclass
class _OperationRecord:
    rec: dict
    __slots__ = ("rec",)


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
        """幂等创建执行记录：同租户同 operation_id 已存在则返回既有（重放），不重复提交外部。"""
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
        rec = self._executions.get(execution_id)
        if rec is None or rec["tenant_id"] != tenant_id:
            raise DomainError(ErrorCode.NOT_FOUND, "执行记录不存在", 404)
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
