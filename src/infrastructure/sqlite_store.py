"""SQLite 持久化存储（可选后端）。

用于本地开发验证"持久化 + 跨连接幂等 + 租户级唯一约束"，证明存储抽象可插拔。
生产目标是 PostgreSQL（见《生产基线与验收测试》§四，含 RLS）；本实现是开发/测试可用的
SQLite 版本，不充当生产数据面。方法与 MemoryStore 签名一致（同步调用）。

注意：本实现为单进程 sqlite3 同步连接，带线程锁；不适用于高并发生产。接入
`STORAGE_BACKEND=sqlite` 时使用；`postgres` 后端仍待 Docker/预发布实现与验证。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
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

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS memberships (
    tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, role TEXT NOT NULL, status TEXT NOT NULL,
    PRIMARY KEY (tenant_id, user_id)
);
CREATE TABLE IF NOT EXISTS sessions (
    tenant_id TEXT NOT NULL, thread_id TEXT NOT NULL, user_id TEXT NOT NULL,
    created_at REAL NOT NULL, expires_at REAL NOT NULL, last_active_at REAL NOT NULL,
    status TEXT NOT NULL, message_count INTEGER NOT NULL DEFAULT 0, title TEXT,
    PRIMARY KEY (tenant_id, thread_id)
);
CREATE TABLE IF NOT EXISTS operations (
    operation_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, thread_id TEXT NOT NULL,
    order_id TEXT NOT NULL, pending_action TEXT NOT NULL, idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL, created_at REAL NOT NULL, result TEXT,
    UNIQUE (tenant_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, thread_id TEXT NOT NULL,
    operation_id TEXT NOT NULL, pending_action TEXT NOT NULL, status TEXT NOT NULL,
    created_at REAL NOT NULL, order_id TEXT, amount REAL, reason TEXT,
    approver TEXT, feedback TEXT, decided_at REAL
);
CREATE TABLE IF NOT EXISTS executions (
    execution_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, operation_id TEXT NOT NULL,
    pending_action TEXT NOT NULL, order_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
    mode TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
    amount REAL, external_txn_id TEXT, callback_nonce TEXT,
    submitted_at REAL, confirmed_at REAL, receipt TEXT, compensation_status TEXT,
    compensation_result TEXT, last_error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
    UNIQUE (tenant_id, operation_id)
);
CREATE INDEX IF NOT EXISTS idx_executions_tenant_status ON executions (tenant_id, status);
CREATE TABLE IF NOT EXISTS streams (
    stream_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
    thread_id TEXT NOT NULL, client_request_id TEXT NOT NULL, mode TEXT NOT NULL,
    created_at REAL NOT NULL, last_seq INTEGER NOT NULL DEFAULT 0, expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS stream_events (
    stream_id TEXT NOT NULL, seq INTEGER NOT NULL, event TEXT NOT NULL, data TEXT NOT NULL,
    created_at REAL NOT NULL, PRIMARY KEY (stream_id, seq)
);
CREATE TABLE IF NOT EXISTS audit (
    audit_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
    action TEXT NOT NULL, target_type TEXT NOT NULL, target_id TEXT NOT NULL,
    detail TEXT NOT NULL, created_at REAL NOT NULL
);
"""


class _QResult:
    """`_q` 的返回：在锁内已物化的查询结果（rows + rowcount）。

    由于 `SqliteStore` 使用单一 sqlite3 连接（check_same_thread=False），
    若把未消费的 cursor 暴露到锁外，其它线程的 DML 会使该 cursor 立刻失效
    （sqlite3 单连接只允许一个活动语句）。因此在锁内 fetchall 物化，保证并发安全。
    """

    __slots__ = ("rows", "rowcount")

    def __init__(self, rows: list, rowcount: int) -> None:
        self.rows = rows
        self.rowcount = rowcount

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list:
        return self.rows


class SqliteStore:
    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True) if str(Path(db_path).parent) != "." else None
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def _q(self, sql: str, args: tuple = ()) -> _QResult:
        with self._lock:
            cur = self._conn.execute(sql, args)
            rows = cur.fetchall()
            rowcount = cur.rowcount
            self._conn.commit()
            return _QResult(rows, rowcount)

    # ---- 租户 / 成员 ----
    def create_tenant(self, tenant_id: str, name: str, status: TenantStatus = TenantStatus.ACTIVE) -> Tenant:
        try:
            self._q("INSERT INTO tenants(id,name,status,created_at) VALUES(?,?,?,?)",
                    (tenant_id, name, status.value, time.time()))
        except sqlite3.IntegrityError:
            raise DomainError(ErrorCode.FORBIDDEN, "租户已存在", 409)
        return self.get_tenant(tenant_id)

    def get_tenant(self, tenant_id: str) -> Tenant:
        row = self._q("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
        if row is None:
            raise DomainError(ErrorCode.NOT_FOUND, "租户不存在", 404)
        return Tenant(id=row["id"], name=row["name"], status=TenantStatus(row["status"]), created_at=row["created_at"])

    def require_active_tenant(self, tenant_id: str) -> Tenant:
        t = self.get_tenant(tenant_id)
        if t.status != TenantStatus.ACTIVE:
            raise DomainError(ErrorCode.TENANT_SUSPENDED, "租户已停用", 403)
        return t

    def set_tenant_status(self, tenant_id: str, status: TenantStatus) -> None:
        cur = self._q("UPDATE tenants SET status=? WHERE id=?", (status.value, tenant_id))
        if cur.rowcount == 0:
            raise DomainError(ErrorCode.NOT_FOUND, "租户不存在", 404)

    def add_membership(self, tenant_id: str, user_id: str, role: Role) -> Membership:
        if role not in Role.tenant_roles():
            raise DomainError(ErrorCode.FORBIDDEN, "非法租户角色", 400)
        self.require_active_tenant(tenant_id)
        try:
            self._q("INSERT INTO memberships(tenant_id,user_id,role,status) VALUES(?,?,?,?)",
                    (tenant_id, user_id, role.value, MembershipStatus.ACTIVE.value))
        except sqlite3.IntegrityError:
            self._q("UPDATE memberships SET role=?,status=? WHERE tenant_id=? AND user_id=?",
                    (role.value, MembershipStatus.ACTIVE.value, tenant_id, user_id))
        return self.get_membership(tenant_id, user_id)

    def get_membership(self, tenant_id: str, user_id: str) -> Optional[Membership]:
        row = self._q("SELECT * FROM memberships WHERE tenant_id=? AND user_id=?",
                      (tenant_id, user_id)).fetchone()
        if row is None:
            return None
        return Membership(tenant_id=row["tenant_id"], user_id=row["user_id"],
                          role=Role(row["role"]), status=MembershipStatus(row["status"]))

    def require_active_membership(self, tenant_id: str, user_id: str) -> Membership:
        self.require_active_tenant(tenant_id)
        m = self.get_membership(tenant_id, user_id)
        if m is None or m.status != MembershipStatus.ACTIVE:
            raise DomainError(ErrorCode.FORBIDDEN, "成员不存在或已撤销", 403)
        return m

    def revoke_membership(self, tenant_id: str, user_id: str) -> None:
        cur = self._q("UPDATE memberships SET status=? WHERE tenant_id=? AND user_id=?",
                      (MembershipStatus.REVOKED.value, tenant_id, user_id))
        if cur.rowcount == 0:
            raise DomainError(ErrorCode.NOT_FOUND, "成员不存在", 404)

    def list_members(self, tenant_id: str) -> list[Membership]:
        rows = self._q("SELECT * FROM memberships WHERE tenant_id=?", (tenant_id,)).fetchall()
        return [Membership(tenant_id=r["tenant_id"], user_id=r["user_id"],
                           role=Role(r["role"]), status=MembershipStatus(r["status"])) for r in rows]

    # ---- 会话 ----
    def create_session(self, tenant_id: str, user_id: str, thread_id: str, now: float, ttl_days: int) -> Session:
        self.require_active_membership(tenant_id, user_id)
        self._q("INSERT INTO sessions(tenant_id,thread_id,user_id,created_at,expires_at,last_active_at,status,message_count,title) "
                "VALUES(?,?,?,?,?,?,?,0,NULL)",
                (tenant_id, thread_id, user_id, now, now + ttl_days * 86400, now, SessionStatus.ACTIVE.value))
        return self.get_session(tenant_id, thread_id)

    def _row_to_session(self, row) -> Session:
        return Session(thread_id=row["thread_id"], tenant_id=row["tenant_id"], user_id=row["user_id"],
                       created_at=row["created_at"], expires_at=row["expires_at"],
                       last_active_at=row["last_active_at"], status=SessionStatus(row["status"]),
                       message_count=row["message_count"], title=row["title"])

    def get_session(self, tenant_id: str, thread_id: str) -> Session:
        row = self._q("SELECT * FROM sessions WHERE tenant_id=? AND thread_id=?", (tenant_id, thread_id)).fetchone()
        if row is None:
            raise DomainError(ErrorCode.NOT_FOUND, "会话不存在", 404)
        return self._row_to_session(row)

    def get_session_or_none(self, tenant_id: str, thread_id: str) -> Optional[Session]:
        try:
            return self.get_session(tenant_id, thread_id)
        except DomainError:
            return None

    def touch_session(self, tenant_id: str, thread_id: str, now: float, ttl_days: int) -> None:
        self._q("UPDATE sessions SET last_active_at=?, expires_at=? WHERE tenant_id=? AND thread_id=?",
                (now, now + ttl_days * 86400, tenant_id, thread_id))

    def increment_message_count(self, tenant_id: str, thread_id: str, delta: int = 1) -> None:
        self._q("UPDATE sessions SET message_count=message_count+? WHERE tenant_id=? AND thread_id=?",
                (delta, tenant_id, thread_id))

    def list_sessions(self, tenant_id: str, user_id: Optional[str] = None) -> list[Session]:
        if user_id is None:
            rows = self._q("SELECT * FROM sessions WHERE tenant_id=?", (tenant_id,)).fetchall()
        else:
            rows = self._q("SELECT * FROM sessions WHERE tenant_id=? AND user_id=?", (tenant_id, user_id)).fetchall()
        return [self._row_to_session(r) for r in rows]

    def mark_session_status(self, tenant_id: str, thread_id: str, status: SessionStatus) -> None:
        self._q("UPDATE sessions SET status=? WHERE tenant_id=? AND thread_id=?", (status.value, tenant_id, thread_id))

    # ---- 业务操作（幂等）----
    def create_operation(self, tenant_id: str, thread_id: str, order_id: str,
                         action: PendingAction, idempotency_key: str, now: float) -> Operation:
        # 先查幂等键：同租户同 idempotency_key 已存在则返回（重放）。
        existing = self._q("SELECT operation_id FROM operations WHERE tenant_id=? AND idempotency_key=?",
                           (tenant_id, idempotency_key)).fetchone()
        if existing is not None:
            return self.get_operation(tenant_id, existing["operation_id"])
        import uuid
        op_id = str(uuid.uuid4())
        self._q("INSERT INTO operations(operation_id,tenant_id,thread_id,order_id,pending_action,idempotency_key,status,created_at,result) "
                "VALUES(?,?,?,?,?,?,?,?,NULL)",
                (op_id, tenant_id, thread_id, order_id, action.value, idempotency_key,
                 OperationStatus.PENDING.value, now))
        return self.get_operation(tenant_id, op_id)

    def get_operation(self, tenant_id: str, operation_id: str) -> Operation:
        row = self._q("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None or row["tenant_id"] != tenant_id:
            raise DomainError(ErrorCode.NOT_FOUND, "操作不存在", 404)
        return Operation(operation_id=row["operation_id"], tenant_id=row["tenant_id"],
                         thread_id=row["thread_id"], order_id=row["order_id"],
                         pending_action=PendingAction(row["pending_action"]),
                         idempotency_key=row["idempotency_key"],
                         status=OperationStatus(row["status"]), created_at=row["created_at"],
                         result=json.loads(row["result"]) if row["result"] else None)

    def update_operation(self, tenant_id: str, operation_id: str, status: OperationStatus,
                         result: Optional[dict] = None) -> Operation:
        cur = self._q("UPDATE operations SET status=?, result=? WHERE operation_id=? AND tenant_id=?",
                      (status.value, json.dumps(result, ensure_ascii=False) if result else None,
                       operation_id, tenant_id))
        if cur.rowcount == 0:
            raise DomainError(ErrorCode.NOT_FOUND, "操作不存在", 404)
        return self.get_operation(tenant_id, operation_id)

    def list_operations(self, tenant_id: str, status: Optional[OperationStatus] = None) -> list[Operation]:
        if status is None:
            rows = self._q("SELECT * FROM operations WHERE tenant_id=?", (tenant_id,)).fetchall()
        else:
            rows = self._q("SELECT * FROM operations WHERE tenant_id=? AND status=?", (tenant_id, status.value)).fetchall()
        return [self._row_to_operation(r) for r in rows]

    def _row_to_operation(self, row) -> Operation:
        return Operation(operation_id=row["operation_id"], tenant_id=row["tenant_id"],
                         thread_id=row["thread_id"], order_id=row["order_id"],
                         pending_action=PendingAction(row["pending_action"]),
                         idempotency_key=row["idempotency_key"],
                         status=OperationStatus(row["status"]), created_at=row["created_at"],
                         result=json.loads(row["result"]) if row["result"] else None)

    # ---- 审批 ----
    def create_approval(self, tenant_id: str, thread_id: str, operation_id: str,
                        action: PendingAction, order_id: Optional[str], amount: Optional[float],
                        reason: Optional[str], now: float) -> Approval:
        import uuid
        approval_id = str(uuid.uuid4())
        self._q("INSERT INTO approvals(approval_id,tenant_id,thread_id,operation_id,pending_action,status,created_at,order_id,amount,reason) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (approval_id, tenant_id, thread_id, operation_id, action.value,
                 ApprovalStatus.PENDING.value, now, order_id, amount, reason))
        return self.get_approval(tenant_id, approval_id)

    def _row_to_approval(self, row) -> Approval:
        return Approval(approval_id=row["approval_id"], tenant_id=row["tenant_id"], thread_id=row["thread_id"],
                        operation_id=row["operation_id"], pending_action=PendingAction(row["pending_action"]),
                        status=ApprovalStatus(row["status"]), created_at=row["created_at"],
                        order_id=row["order_id"], amount=row["amount"], reason=row["reason"],
                        approver=row["approver"], feedback=row["feedback"], decided_at=row["decided_at"])

    def get_approval(self, tenant_id: str, approval_id: str) -> Approval:
        row = self._q("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
        if row is None or row["tenant_id"] != tenant_id:
            raise DomainError(ErrorCode.NOT_FOUND, "审批单不存在", 404)
        return self._row_to_approval(row)

    def list_approvals(self, tenant_id: str, status: Optional[ApprovalStatus] = None) -> list[Approval]:
        if status is None:
            rows = self._q("SELECT * FROM approvals WHERE tenant_id=?", (tenant_id,)).fetchall()
        else:
            rows = self._q("SELECT * FROM approvals WHERE tenant_id=? AND status=?", (tenant_id, status.value)).fetchall()
        return [self._row_to_approval(r) for r in rows]

    def decide_approval(self, tenant_id: str, approval_id: str, approver: str,
                        approved: bool, feedback: Optional[str], now: float) -> Approval:
        """审批决策（幂等重放）。内部走 CAS；已决策则返回既有结果，不重复生效。"""
        approval, _claimed = self.claim_approval_decision(
            tenant_id, approval_id, approver, approved, feedback, now)
        return approval

    def claim_approval_decision(self, tenant_id: str, approval_id: str, approver: str,
                                approved: bool, feedback: Optional[str],
                                now: float) -> tuple[Approval, bool]:
        """CAS 原子抢占 pending -> 终态（approved/rejected）。

        单条 `UPDATE ... WHERE status='pending'` 原子完成；为让单连接存储下的
        「读-改-写-回读」作为一个不可分割临界区，整个方法在同一连接锁内执行，
        避免并发时读到缺失状态（单写者 CAS 保证）。
        """
        with self._lock:
            row = self._q("SELECT operation_id FROM approvals WHERE approval_id=? AND tenant_id=?",
                          (approval_id, tenant_id)).fetchone()
            if row is None:
                raise DomainError(ErrorCode.NOT_FOUND, "审批单不存在", 404)
            new_status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
            cur = self._q(
                "UPDATE approvals SET status=?, approver=?, feedback=?, decided_at=? "
                "WHERE approval_id=? AND tenant_id=? AND status='pending'",
                (new_status.value, approver, feedback, now, approval_id, tenant_id),
            )
            claimed = cur.rowcount == 1
            if claimed:
                self._apply_approval_decision_to_operation(tenant_id, row["operation_id"],
                                                           approved, feedback, now)
            approval = self.get_approval(tenant_id, approval_id)
        return approval, claimed

    def _apply_approval_decision_to_operation(self, tenant_id: str, operation_id: str,
                                              approved: bool, feedback: Optional[str],
                                              now: float) -> None:
        """审批终态联动 operation 状态。"""
        if approved:
            return  # 通过：operation 保持 pending，由 execute 置 executed。
        op = self._q("SELECT status FROM operations WHERE operation_id=? AND tenant_id=?",
                     (operation_id, tenant_id)).fetchone()
        if op is None or op["status"] in (OperationStatus.EXECUTED.value, OperationStatus.HUMAN_HANDOFF.value):
            return
        new_status = (OperationStatus.HUMAN_HANDOFF.value
                      if self._feedback_requests_handoff(feedback)
                      else OperationStatus.REJECTED.value)
        self._q("UPDATE operations SET status=? WHERE operation_id=? AND tenant_id=?",
                (new_status, operation_id, tenant_id))

    @staticmethod
    def _feedback_requests_handoff(feedback: Optional[str]) -> bool:
        if not feedback:
            return False
        return any(k in feedback for k in ("人工", "转人工", "人工处理", "human", "handoff"))

    def expire_stale_approvals(self, tenant_id: str, now: float, timeout_seconds: float) -> list[str]:
        """审批超时恢复：把超过 timeout_seconds 仍未决策的 pending 审批标记为 timeout。

        对应 operation 置 human_handoff（保留 operation_id 供人工接管），禁止自动放行或重建
        新 operation_id。返回本次过期的 approval_id 列表。
        """
        rows = self._q(
            "SELECT approval_id, operation_id FROM approvals "
            "WHERE tenant_id=? AND status='pending' AND created_at<=?",
            (tenant_id, now - timeout_seconds),
        ).fetchall()
        expired: list[str] = []
        for r in rows:
            self._q("UPDATE approvals SET status=?, decided_at=? "
                    "WHERE approval_id=? AND status='pending'",
                    (ApprovalStatus.TIMEOUT.value, now, r["approval_id"]))
            op = self._q("SELECT status FROM operations WHERE operation_id=? AND tenant_id=?",
                         (r["operation_id"], tenant_id)).fetchone()
            if op is not None and op["status"] != OperationStatus.EXECUTED.value:
                self._q("UPDATE operations SET status=? WHERE operation_id=? AND tenant_id=?",
                        (OperationStatus.HUMAN_HANDOFF.value, r["operation_id"], tenant_id))
            expired.append(r["approval_id"])
        return expired

    # ---- 执行记录（资金/业务执行面，幂等锚点 = operation_id）----
    def create_execution_record(self, tenant_id: str, *, operation_id: str,
                                pending_action: PendingAction, order_id: str,
                                idempotency_key: str, mode: ExecutionMode, amount: float | None,
                                now: float) -> ExecutionRecord:
        existing = self._q("SELECT execution_id FROM executions WHERE tenant_id=? AND operation_id=?",
                           (tenant_id, operation_id)).fetchone()
        if existing is not None:
            return self.get_execution_record(tenant_id, existing["execution_id"])
        import uuid
        exec_id = str(uuid.uuid4())
        try:
            self._q(
                "INSERT INTO executions(execution_id,tenant_id,operation_id,pending_action,order_id,"
                "idempotency_key,mode,status,created_at,updated_at,amount,attempts) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,0)",
                (exec_id, tenant_id, operation_id, pending_action.value, order_id, idempotency_key,
                 mode.value, ExecutionStatus.PENDING_SUBMIT.value, now, now, amount),
            )
        except sqlite3.IntegrityError:
            # 并发下同 operation 已插入 → 返回既有（幂等重放）。
            existing = self._q("SELECT execution_id FROM executions WHERE tenant_id=? AND operation_id=?",
                               (tenant_id, operation_id)).fetchone()
            return self.get_execution_record(tenant_id, existing["execution_id"])
        return self.get_execution_record(tenant_id, exec_id)

    def get_execution_record(self, tenant_id: str, execution_id: str) -> ExecutionRecord:
        row = self._q("SELECT * FROM executions WHERE execution_id=?", (execution_id,)).fetchone()
        if row is None or row["tenant_id"] != tenant_id:
            raise DomainError(ErrorCode.NOT_FOUND, "执行记录不存在", 404)
        return self._row_to_execution(row)

    def get_execution_by_operation(self, tenant_id: str, operation_id: str) -> ExecutionRecord | None:
        row = self._q("SELECT * FROM executions WHERE tenant_id=? AND operation_id=?",
                      (tenant_id, operation_id)).fetchone()
        return self._row_to_execution(row) if row else None

    def list_execution_records(self, tenant_id: str,
                               status: ExecutionStatus | None = None) -> list[ExecutionRecord]:
        if status is None:
            rows = self._q("SELECT * FROM executions WHERE tenant_id=?", (tenant_id,)).fetchall()
        else:
            rows = self._q("SELECT * FROM executions WHERE tenant_id=? AND status=?",
                           (tenant_id, status.value)).fetchall()
        return [self._row_to_execution(r) for r in rows]

    def update_execution_record(self, tenant_id: str, execution_id: str,
                                **fields) -> ExecutionRecord:
        cols, args = [], []
        for k, v in fields.items():
            if k in ("status", "mode") and isinstance(v, (ExecutionStatus, ExecutionMode)):
                v = v.value
            if k in ("receipt", "compensation_result") and v is not None:
                v = json.dumps(v, ensure_ascii=False)
            cols.append(f"{k}=?")
            args.append(v)
        args += [execution_id, tenant_id]
        cur = self._q(f"UPDATE executions SET {', '.join(cols)} "
                      "WHERE execution_id=? AND tenant_id=?", tuple(args))
        if cur.rowcount == 0:
            raise DomainError(ErrorCode.NOT_FOUND, "执行记录不存在", 404)
        return self.get_execution_record(tenant_id, execution_id)

    def claim_callback(self, tenant_id: str, execution_id: str,
                       nonce: str) -> tuple[ExecutionRecord, bool]:
        with self._lock:
            row = self._q("SELECT callback_nonce FROM executions WHERE execution_id=? AND tenant_id=?",
                          (execution_id, tenant_id)).fetchone()
            if row is None:
                raise DomainError(ErrorCode.NOT_FOUND, "执行记录不存在", 404)
            if row["callback_nonce"] is None:
                self._q("UPDATE executions SET callback_nonce=?, updated_at=? "
                        "WHERE execution_id=? AND tenant_id=?",
                        (nonce, time.time(), execution_id, tenant_id))
                return self.get_execution_record(tenant_id, execution_id), False
            if row["callback_nonce"] == nonce:
                return self.get_execution_record(tenant_id, execution_id), True
            self._q("UPDATE executions SET callback_nonce=?, updated_at=? "
                    "WHERE execution_id=? AND tenant_id=?",
                    (nonce, time.time(), execution_id, tenant_id))
            return self.get_execution_record(tenant_id, execution_id), False

    def _row_to_execution(self, row) -> ExecutionRecord:
        return ExecutionRecord(
            execution_id=row["execution_id"], tenant_id=row["tenant_id"],
            operation_id=row["operation_id"], pending_action=PendingAction(row["pending_action"]),
            order_id=row["order_id"], idempotency_key=row["idempotency_key"],
            mode=ExecutionMode(row["mode"]), status=ExecutionStatus(row["status"]),
            created_at=row["created_at"], updated_at=row["updated_at"], amount=row["amount"],
            external_txn_id=row["external_txn_id"], callback_nonce=row["callback_nonce"],
            submitted_at=row["submitted_at"], confirmed_at=row["confirmed_at"],
            receipt=json.loads(row["receipt"]) if row["receipt"] else None,
            compensation_status=row["compensation_status"],
            compensation_result=json.loads(row["compensation_result"]) if row["compensation_result"] else None,
            last_error=row["last_error"], attempts=row["attempts"],
        )

    # ---- SSE 流 ----
    def create_stream(self, stream_id: str, tenant_id: str, user_id: str, thread_id: str,
                      client_request_id: str, mode: str, now: float) -> None:
        self._q("INSERT INTO streams(stream_id,tenant_id,user_id,thread_id,client_request_id,mode,created_at,last_seq,expires_at) "
                "VALUES(?,?,?,?,?,?,?,0,?)",
                (stream_id, tenant_id, user_id, thread_id, client_request_id, mode, now, now + 7 * 86400))

    def find_stream_by_client(self, tenant_id: str, user_id: str, client_request_id: str) -> Optional[str]:
        row = self._q("SELECT stream_id FROM streams WHERE tenant_id=? AND user_id=? AND client_request_id=?",
                      (tenant_id, user_id, client_request_id)).fetchone()
        return row["stream_id"] if row else None

    def get_stream(self, tenant_id: str, stream_id: str) -> dict:
        row = self._q("SELECT * FROM streams WHERE stream_id=?", (stream_id,)).fetchone()
        if row is None or row["tenant_id"] != tenant_id:
            raise DomainError(ErrorCode.NOT_FOUND, "流不存在", 404)
        return dict(row)

    def append_event(self, tenant_id: str, stream_id: str, event: str, data: dict, now: float) -> int:
        stream = self.get_stream(tenant_id, stream_id)
        seq = stream["last_seq"] + 1
        # 原子更新 last_seq（读-改-写在同一锁内）。
        self._q("UPDATE streams SET last_seq=? WHERE stream_id=?", (seq, stream_id))
        self._q("INSERT INTO stream_events(stream_id,seq,event,data,created_at) VALUES(?,?,?,?,?)",
                (stream_id, seq, event, json.dumps(data, ensure_ascii=False), now))
        return seq

    def events_after(self, tenant_id: str, stream_id: str, last_seq: int) -> list[dict]:
        self.get_stream(tenant_id, stream_id)
        rows = self._q("SELECT * FROM stream_events WHERE stream_id=? AND seq>? ORDER BY seq",
                       (stream_id, last_seq)).fetchall()
        return [{"stream_id": r["stream_id"], "seq": r["seq"], "event": r["event"],
                 "data": json.loads(r["data"]), "created_at": r["created_at"]} for r in rows]

    # ---- 审计 ----
    def append_audit(self, tenant_id: str, user_id: str, action: str, target_type: str,
                     target_id: str, detail: dict, now: float) -> AuditRecord:
        import uuid
        audit_id = str(uuid.uuid4())
        self._q("INSERT INTO audit(audit_id,tenant_id,user_id,action,target_type,target_id,detail,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (audit_id, tenant_id, user_id, action, target_type, target_id,
                 json.dumps(detail, ensure_ascii=False), now))
        return AuditRecord(audit_id=audit_id, tenant_id=tenant_id, user_id=user_id, action=action,
                           target_type=target_type, target_id=target_id, detail=detail, created_at=now)

    def list_audit(self, tenant_id: str) -> list[AuditRecord]:
        rows = self._q("SELECT * FROM audit WHERE tenant_id=?", (tenant_id,)).fetchall()
        return [AuditRecord(audit_id=r["audit_id"], tenant_id=r["tenant_id"], user_id=r["user_id"],
                            action=r["action"], target_type=r["target_type"], target_id=r["target_id"],
                            detail=json.loads(r["detail"]), created_at=r["created_at"]) for r in rows]

    def search_audit(self, tenant_id: str, **filters) -> list[AuditRecord]:
        from src.infrastructure.store import filter_audit
        return filter_audit(self.list_audit(tenant_id), **filters)
