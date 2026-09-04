"""PostgreSQL 持久化存储（生产数据面）。

与 MemoryStore/SqliteStore 的公有方法签名一致（同步调用）。核心安全属性：
- **RLS 同事务闭环**：每个业务方法在 `engine.begin()` 开启的事务内、以
  `set_config('app.tenant_id', tenant, true)`（事务本地）设置租户作用域后再执行 SQL，
  与数据库 RLS policy 使用同一连接与同一事务，杜绝"先在业务连接 SET、再由别的连接执行"。
- **跨租户零可见/零可写**：应用层按 `tenant_id` 参数过滤 + 数据库 RLS 兜底；
  缺少租户作用域（未设置 app.tenant_id）时 RLS 使其零可见。
- **幂等**：`UNIQUE(tenant_id, idempotency_key)` + `INSERT ... ON CONFLICT DO NOTHING`。
- `tenants` 为平台主数据不启用 RLS（由平台/迁移角色维护），其余业务表全部启用。

时间戳约定：与领域对象一致，使用 DOUBLE PRECISION 存 Unix 秒（time.time()）。
JSONB 字段（result/data/detail）：写入 json.dumps，读取 json.loads。
"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from typing import Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, Connection

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
)

_TERMINAL_EXECUTION_STATUSES = (
    ExecutionStatus.CONFIRMED.value,
    ExecutionStatus.COMPENSATED.value,
    ExecutionStatus.COMPENSATION_FAILED.value,
    ExecutionStatus.RECONCILED.value,
    ExecutionStatus.MISMATCHED.value,
    ExecutionStatus.HUMAN_HANDOFF.value,
)


def _as_json(value):
    """安全解析 JSONB 列值。

    psycopg3 驱动会按 PostgreSQL 列类型自动把 JSONB/JSON 列解析为 Python dict/list；
    SQLAlchemy 在读取这些列时不会再次处理。因此用本助手仅在值为 str/bytes 时才
    json.loads，已经是 dict/list（或 None）则原样返回，避免对已解析对象重复 json.loads
    抛 'the JSON object must be str, bytes or bytearray, not dict'。
    """
    if value is None:
        return None
    if isinstance(value, (str, bytes, bytearray)):
        return json.loads(value)
    return value


class PostgresStore:
    def __init__(self, database_url: str, *, engine: Engine | None = None) -> None:
        if engine is not None:
            self._engine = engine
        else:
            self._engine = create_engine(_to_sqlalchemy_url(database_url), pool_pre_ping=True)
        self.engine = self._engine
        self._checkpointer = None  # 由 runtime 组装时挂载（供清理任务取用）

    def close(self) -> None:
        self._engine.dispose()

    # -- 事务 helper：同一事务内设置 RLS 作用域 --
    @contextmanager
    def _tx(self, tenant_id: str | None = None, *, write: bool = False):
        # 使用 begin() 开启显式事务；set_config(..., true) 为事务本地，保证同一事务内生效。
        with self._engine.begin() as conn:
            if tenant_id is not None:
                conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
            yield conn

    # ---- 租户 / 成员 ----
    def create_tenant(self, tenant_id: str, name: str, status: TenantStatus = TenantStatus.ACTIVE) -> Tenant:
        with self._tx() as conn:
            try:
                conn.execute(
                    text("INSERT INTO tenants(id,name,status,created_at) VALUES(:id,:name,:status,:ts)"),
                    {"id": tenant_id, "name": name, "status": status.value, "ts": time.time()},
                )
            except Exception as exc:  # 唯一冲突
                raise DomainError(ErrorCode.FORBIDDEN, "租户已存在", 409) from exc
        return self.get_tenant(tenant_id)

    def get_tenant(self, tenant_id: str) -> Tenant:
        with self._tx() as conn:
            row = conn.execute(
                text("SELECT id,name,status,created_at FROM tenants WHERE id=:id"), {"id": tenant_id}
            ).mappings().first()
        if row is None:
            raise DomainError(ErrorCode.NOT_FOUND, "租户不存在", 404)
        return Tenant(id=row["id"], name=row["name"], status=TenantStatus(row["status"]),
                      created_at=row["created_at"])

    def require_active_tenant(self, tenant_id: str) -> Tenant:
        t = self.get_tenant(tenant_id)
        if t.status != TenantStatus.ACTIVE:
            raise DomainError(ErrorCode.TENANT_SUSPENDED, "租户已停用", 403)
        return t

    def set_tenant_status(self, tenant_id: str, status: TenantStatus) -> None:
        with self._tx() as conn:
            res = conn.execute(
                text("UPDATE tenants SET status=:s WHERE id=:id"), {"s": status.value, "id": tenant_id}
            )
            if res.rowcount == 0:
                raise DomainError(ErrorCode.NOT_FOUND, "租户不存在", 404)

    def add_membership(self, tenant_id: str, user_id: str, role: Role) -> Membership:
        if role not in Role.tenant_roles():
            raise DomainError(ErrorCode.FORBIDDEN, "非法租户角色", 400)
        self.require_active_tenant(tenant_id)
        with self._tx(tenant_id) as conn:
            conn.execute(
                text("INSERT INTO memberships(tenant_id,user_id,role,status) "
                     "VALUES(:t,:u,:r,:s) ON CONFLICT (tenant_id,user_id) "
                     "DO UPDATE SET role=EXCLUDED.role, status=EXCLUDED.status"),
                {"t": tenant_id, "u": user_id, "r": role.value, "s": MembershipStatus.ACTIVE.value},
            )
        return self.get_membership(tenant_id, user_id)  # type: ignore[return-value]

    def get_membership(self, tenant_id: str, user_id: str) -> Optional[Membership]:
        with self._tx(tenant_id) as conn:
            row = conn.execute(
                text("SELECT tenant_id,user_id,role,status FROM memberships WHERE tenant_id=:t AND user_id=:u"),
                {"t": tenant_id, "u": user_id},
            ).mappings().first()
        if row is None:
            return None
        return Membership(tenant_id=row["tenant_id"], user_id=row["user_id"], role=Role(row["role"]),
                          status=MembershipStatus(row["status"]))

    def require_active_membership(self, tenant_id: str, user_id: str) -> Membership:
        self.require_active_tenant(tenant_id)
        m = self.get_membership(tenant_id, user_id)
        if m is None or m.status != MembershipStatus.ACTIVE:
            raise DomainError(ErrorCode.FORBIDDEN, "成员不存在或已撤销", 403)
        return m

    def revoke_membership(self, tenant_id: str, user_id: str) -> None:
        with self._tx(tenant_id) as conn:
            res = conn.execute(
                text("UPDATE memberships SET status=:s WHERE tenant_id=:t AND user_id=:u"),
                {"s": MembershipStatus.REVOKED.value, "t": tenant_id, "u": user_id},
            )
            if res.rowcount == 0:
                raise DomainError(ErrorCode.NOT_FOUND, "成员不存在", 404)

    def list_members(self, tenant_id: str) -> list[Membership]:
        with self._tx(tenant_id) as conn:
            rows = conn.execute(
                text("SELECT tenant_id,user_id,role,status FROM memberships WHERE tenant_id=:t"),
                {"t": tenant_id},
            ).mappings().all()
        return [Membership(tenant_id=r["tenant_id"], user_id=r["user_id"], role=Role(r["role"]),
                           status=MembershipStatus(r["status"])) for r in rows]

    # ---- 会话 ----
    def create_session(self, tenant_id: str, user_id: str, thread_id: str, now: float,
                       ttl_days: int) -> Session:
        self.require_active_membership(tenant_id, user_id)
        expires_at = now + ttl_days * 86400
        with self._tx(tenant_id, write=True) as conn:
            conn.execute(
                text("INSERT INTO sessions(tenant_id,thread_id,user_id,created_at,expires_at,"
                     "last_active_at,status,message_count,title) "
                     "VALUES(:t,:th,:u,:ca,:ea,:la,:s,0,NULL)"),
                {"t": tenant_id, "th": thread_id, "u": user_id, "ca": now, "ea": expires_at,
                 "la": now, "s": SessionStatus.ACTIVE.value},
            )
            # 同一事务内创建 checkpoint scope（权威映射），保证 session+scope 原子创建。
            conn.execute(
                text("INSERT INTO checkpoint_thread_scopes(tenant_id,thread_id,created_at,last_active_at,"
                     "expires_at,status,cleanup_attempts) VALUES(:t,:th,:ca,:la,:ea,'active',0) "
                     "ON CONFLICT (thread_id) DO NOTHING"),
                {"t": tenant_id, "th": thread_id, "ca": now, "la": now, "ea": expires_at},
            )
        return self.get_session(tenant_id, thread_id)

    def _row_to_session(self, row) -> Session:
        return Session(thread_id=row["thread_id"], tenant_id=row["tenant_id"], user_id=row["user_id"],
                       created_at=row["created_at"], expires_at=row["expires_at"],
                       last_active_at=row["last_active_at"], status=SessionStatus(row["status"]),
                       message_count=row["message_count"], title=row["title"])

    def get_session(self, tenant_id: str, thread_id: str) -> Session:
        with self._tx(tenant_id) as conn:
            row = conn.execute(
                text("SELECT * FROM sessions WHERE tenant_id=:t AND thread_id=:th"),
                {"t": tenant_id, "th": thread_id},
            ).mappings().first()
        if row is None:
            raise DomainError(ErrorCode.NOT_FOUND, "会话不存在", 404)
        return self._row_to_session(row)

    def get_session_or_none(self, tenant_id: str, thread_id: str) -> Optional[Session]:
        try:
            return self.get_session(tenant_id, thread_id)
        except DomainError:
            return None

    def touch_session(self, tenant_id: str, thread_id: str, now: float, ttl_days: int) -> None:
        with self._tx(tenant_id, write=True) as conn:
            res = conn.execute(
                text("UPDATE sessions SET last_active_at=:la, expires_at=:ea "
                     "WHERE tenant_id=:t AND thread_id=:th"),
                {"la": now, "ea": now + ttl_days * 86400, "t": tenant_id, "th": thread_id},
            )
            if res.rowcount == 0:
                raise DomainError(ErrorCode.NOT_FOUND, "会话不存在", 404)

    def increment_message_count(self, tenant_id: str, thread_id: str, delta: int = 1) -> None:
        with self._tx(tenant_id, write=True) as conn:
            conn.execute(
                text("UPDATE sessions SET message_count=message_count+:d "
                     "WHERE tenant_id=:t AND thread_id=:th"),
                {"d": delta, "t": tenant_id, "th": thread_id},
            )

    def list_sessions(self, tenant_id: str, user_id: Optional[str] = None) -> list[Session]:
        with self._tx(tenant_id) as conn:
            if user_id is None:
                rows = conn.execute(
                    text("SELECT * FROM sessions WHERE tenant_id=:t"), {"t": tenant_id}
                ).mappings().all()
            else:
                rows = conn.execute(
                    text("SELECT * FROM sessions WHERE tenant_id=:t AND user_id=:u"),
                    {"t": tenant_id, "u": user_id},
                ).mappings().all()
        return [self._row_to_session(r) for r in rows]

    def mark_session_status(self, tenant_id: str, thread_id: str, status: SessionStatus) -> None:
        with self._tx(tenant_id, write=True) as conn:
            conn.execute(
                text("UPDATE sessions SET status=:s WHERE tenant_id=:t AND thread_id=:th"),
                {"s": status.value, "t": tenant_id, "th": thread_id},
            )

    # ---- 业务操作（幂等）----
    def create_operation(self, tenant_id: str, thread_id: str, order_id: str,
                         action: PendingAction, idempotency_key: str, now: float) -> Operation:
        import uuid
        op_id = str(uuid.uuid4())
        with self._tx(tenant_id, write=True) as conn:
            conn.execute(
                text("INSERT INTO operations(operation_id,tenant_id,thread_id,order_id,pending_action,"
                     "idempotency_key,status,created_at,result) "
                     "VALUES(:oid,:t,:th,:ord,:pa,:ik,:s,:ca,NULL) "
                     "ON CONFLICT (tenant_id,idempotency_key) DO NOTHING"),
                {"oid": op_id, "t": tenant_id, "th": thread_id, "ord": order_id,
                 "pa": action.value, "ik": idempotency_key,
                 "s": OperationStatus.PENDING.value, "ca": now},
            )
            row = conn.execute(
                text("SELECT operation_id FROM operations WHERE tenant_id=:t AND idempotency_key=:ik"),
                {"t": tenant_id, "ik": idempotency_key},
            ).mappings().first()
        if row is None:
            raise DomainError(ErrorCode.INTERNAL_ERROR, "幂等写入失败", 500)
        return self.get_operation(tenant_id, row["operation_id"])

    def get_operation(self, tenant_id: str, operation_id: str) -> Operation:
        with self._tx(tenant_id) as conn:
            row = conn.execute(
                text("SELECT * FROM operations WHERE operation_id=:oid AND tenant_id=:t"),
                {"oid": operation_id, "t": tenant_id},
            ).mappings().first()
        if row is None:
            raise DomainError(ErrorCode.NOT_FOUND, "操作不存在", 404)
        return self._row_to_operation(row)

    def update_operation(self, tenant_id: str, operation_id: str, status: OperationStatus,
                         result: Optional[dict] = None) -> Operation:
        with self._tx(tenant_id, write=True) as conn:
            res = conn.execute(
                text("UPDATE operations SET status=:s, result=:r "
                     "WHERE operation_id=:oid AND tenant_id=:t"),
                {"s": status.value, "r": json.dumps(result, ensure_ascii=False) if result else None,
                 "oid": operation_id, "t": tenant_id},
            )
            if res.rowcount == 0:
                raise DomainError(ErrorCode.NOT_FOUND, "操作不存在", 404)
        return self.get_operation(tenant_id, operation_id)

    def list_operations(self, tenant_id: str, status: Optional[OperationStatus] = None) -> list[Operation]:
        with self._tx(tenant_id) as conn:
            if status is None:
                rows = conn.execute(
                    text("SELECT * FROM operations WHERE tenant_id=:t"), {"t": tenant_id}
                ).mappings().all()
            else:
                rows = conn.execute(
                    text("SELECT * FROM operations WHERE tenant_id=:t AND status=:s"),
                    {"t": tenant_id, "s": status.value},
                ).mappings().all()
        return [self._row_to_operation(r) for r in rows]

    def _row_to_operation(self, row) -> Operation:
        return Operation(operation_id=row["operation_id"], tenant_id=row["tenant_id"],
                         thread_id=row["thread_id"], order_id=row["order_id"],
                         pending_action=PendingAction(row["pending_action"]),
                         idempotency_key=row["idempotency_key"],
                         status=OperationStatus(row["status"]), created_at=row["created_at"],
                         result=_as_json(row["result"]))

    # ---- 审批 ----
    def create_approval(self, tenant_id: str, thread_id: str, operation_id: str,
                        action: PendingAction, order_id: Optional[str], amount: Optional[float],
                        reason: Optional[str], now: float) -> Approval:
        import uuid
        approval_id = str(uuid.uuid4())
        with self._tx(tenant_id, write=True) as conn:
            # 幂等：同一租户同一操作已有审批单则重放（ON CONFLICT DO NOTHING）。
            conn.execute(
                text("INSERT INTO approvals(approval_id,tenant_id,thread_id,operation_id,pending_action,"
                     "status,created_at,order_id,amount,reason) VALUES(:aid,:t,:th,:op,:pa,:s,:ca,:ord,:amt,:rs) "
                     "ON CONFLICT (tenant_id,operation_id) DO NOTHING"),
                {"aid": approval_id, "t": tenant_id, "th": thread_id, "op": operation_id,
                 "pa": action.value, "s": ApprovalStatus.PENDING.value, "ca": now,
                 "ord": order_id, "amt": amount, "rs": reason},
            )
            row = conn.execute(
                text("SELECT approval_id FROM approvals WHERE tenant_id=:t AND operation_id=:op"),
                {"t": tenant_id, "op": operation_id},
            ).mappings().first()
        if row is None:
            raise DomainError(ErrorCode.INTERNAL_ERROR, "审批幂等写入失败", 500)
        return self.get_approval(tenant_id, row["approval_id"])

    def _row_to_approval(self, row) -> Approval:
        return Approval(approval_id=row["approval_id"], tenant_id=row["tenant_id"],
                        thread_id=row["thread_id"], operation_id=row["operation_id"],
                        pending_action=PendingAction(row["pending_action"]),
                        status=ApprovalStatus(row["status"]), created_at=row["created_at"],
                        order_id=row["order_id"], amount=row["amount"], reason=row["reason"],
                        approver=row["approver"], feedback=row["feedback"], decided_at=row["decided_at"])

    def get_approval(self, tenant_id: str, approval_id: str) -> Approval:
        with self._tx(tenant_id) as conn:
            row = conn.execute(
                text("SELECT * FROM approvals WHERE approval_id=:aid AND tenant_id=:t"),
                {"aid": approval_id, "t": tenant_id},
            ).mappings().first()
        if row is None:
            raise DomainError(ErrorCode.NOT_FOUND, "审批单不存在", 404)
        return self._row_to_approval(row)

    def list_approvals(self, tenant_id: str, status: Optional[ApprovalStatus] = None) -> list[Approval]:
        with self._tx(tenant_id) as conn:
            if status is None:
                rows = conn.execute(
                    text("SELECT * FROM approvals WHERE tenant_id=:t"), {"t": tenant_id}
                ).mappings().all()
            else:
                rows = conn.execute(
                    text("SELECT * FROM approvals WHERE tenant_id=:t AND status=:s"),
                    {"t": tenant_id, "s": status.value},
                ).mappings().all()
        return [self._row_to_approval(r) for r in rows]

    def decide_approval(self, tenant_id: str, approval_id: str, approver: str,
                        approved: bool, feedback: Optional[str], now: float) -> Approval:
        with self._tx(tenant_id, write=True) as conn:
            row = conn.execute(
                text("SELECT status FROM approvals WHERE approval_id=:aid AND tenant_id=:t"),
                {"aid": approval_id, "t": tenant_id},
            ).mappings().first()
            if row is None:
                raise DomainError(ErrorCode.NOT_FOUND, "审批单不存在", 404)
            if row["status"] == ApprovalStatus.PENDING.value:
                new_status = ApprovalStatus.APPROVED.value if approved else ApprovalStatus.REJECTED.value
                conn.execute(
                    text("UPDATE approvals SET status=:s, approver=:ap, feedback=:fb, decided_at=:da "
                         "WHERE approval_id=:aid"),
                    {"s": new_status, "ap": approver, "fb": feedback, "da": now, "aid": approval_id},
                )
        return self.get_approval(tenant_id, approval_id)

    def claim_approval_decision(self, tenant_id: str, approval_id: str, approver: str,
                                approved: bool, feedback: Optional[str],
                                now: float) -> tuple[Approval, bool]:
        """CAS 原子抢占 pending -> 终态（approved/rejected），并在同一事务联动 operation。

        返回 (Approval, claimed)：claimed=True 本次真正抢占；False 为已决策/重放，
        调用方不得恢复图执行，避免双执行。
        """
        new_status = ApprovalStatus.APPROVED.value if approved else ApprovalStatus.REJECTED.value
        with self._tx(tenant_id, write=True) as conn:
            row = conn.execute(
                text("UPDATE approvals SET status=:s, approver=:ap, feedback=:fb, decided_at=:da "
                     "WHERE approval_id=:aid AND tenant_id=:t AND status='pending' "
                     "RETURNING operation_id"),
                {"s": new_status, "ap": approver, "fb": feedback, "da": now,
                 "aid": approval_id, "t": tenant_id},
            ).mappings().first()
            if row is None:
                return self.get_approval(tenant_id, approval_id), False  # 已决策：重放
            op_id = row["operation_id"]
            # 拒绝：联动 operation → rejected / human_handoff；通过：保持 pending（由 execute 置 executed）。
            if not approved:
                op_status = (OperationStatus.HUMAN_HANDOFF.value
                             if self._feedback_requests_handoff(feedback)
                             else OperationStatus.REJECTED.value)
                conn.execute(
                    text("UPDATE operations SET status=:s "
                         "WHERE operation_id=:oid AND tenant_id=:t AND status<>'executed'"),
                    {"s": op_status, "oid": op_id, "t": tenant_id},
                )
        return self.get_approval(tenant_id, approval_id), True

    @staticmethod
    def _feedback_requests_handoff(feedback: Optional[str]) -> bool:
        """拒绝反馈是否请求转人工（决定 operation 标记 rejected 还是 human_handoff）。"""
        if not feedback:
            return False
        return any(k in feedback for k in ("人工", "转人工", "人工处理", "human", "handoff"))

    def expire_stale_approvals(self, tenant_id: str, now: float,
                               timeout_seconds: float) -> list[str]:
        """审批超时恢复：把超时未决策的 pending 审批标记为 timeout；对应 operation 转人工。

        保留 operation_id 供人工接管；禁止自动放行或重建 operation_id。返回本次过期列表。
        """
        expired: list[str] = []
        with self._tx(tenant_id, write=True) as conn:
            rows = conn.execute(
                text("UPDATE approvals SET status='timeout', decided_at=:now "
                     "WHERE tenant_id=:t AND status='pending' AND created_at + :to <= :now "
                     "RETURNING approval_id, operation_id"),
                {"t": tenant_id, "now": now, "to": timeout_seconds},
            ).mappings().all()
            for r in rows:
                conn.execute(
                    text("UPDATE operations SET status='human_handoff' "
                         "WHERE operation_id=:oid AND tenant_id=:t AND status<>'executed'"),
                    {"oid": r["operation_id"], "t": tenant_id},
                )
                expired.append(r["approval_id"])
        return expired

    # ---- 执行记录（资金/业务执行面，幂等锚点 = operation_id）----
    def create_execution_record(self, tenant_id: str, *, operation_id: str,
                                pending_action: PendingAction, order_id: str,
                                idempotency_key: str, mode: ExecutionMode, amount: float | None,
                                now: float) -> ExecutionRecord:
        import uuid
        exec_id = str(uuid.uuid4())
        with self._tx(tenant_id, write=True) as conn:
            conn.execute(
                text("INSERT INTO executions(execution_id,tenant_id,operation_id,pending_action,"
                     "order_id,idempotency_key,mode,status,created_at,updated_at,amount,attempts) "
                     "VALUES(:eid,:t,:oid,:pa,:ord,:ik,:m,:s,:ca,:ua,:amt,0) "
                     "ON CONFLICT (tenant_id,operation_id) DO NOTHING"),
                {"eid": exec_id, "t": tenant_id, "oid": operation_id, "pa": pending_action.value,
                 "ord": order_id, "ik": idempotency_key, "m": mode.value,
                 "s": ExecutionStatus.PENDING_SUBMIT.value, "ca": now, "ua": now, "amt": amount},
            )
            row = conn.execute(
                text("SELECT execution_id FROM executions WHERE tenant_id=:t AND operation_id=:oid"),
                {"t": tenant_id, "oid": operation_id},
            ).mappings().first()
            if row is None:
                raise DomainError(ErrorCode.INTERNAL_ERROR, "执行记录幂等写入失败", 500)
        return self.get_execution_record(tenant_id, row["execution_id"])

    def claim_execution_submit(self, tenant_id: str, execution_id: str, *, now: float | None = None) -> bool:
        """单执行守卫：原子抢占"本次 live 提交外部"的权利，仅一个线程成功（RLS + FOR UPDATE）。

        用 attempts 0→1 作为单次认领标记（作用于 status=pending_submit 的非终态记录）。同一事务、
        同一连接内：`FOR UPDATE` 锁定执行行（+ RLS 强制租户作用域），再以
        `WHERE status='pending_submit' AND attempts=0` 做 CAS；命中 rowcount=1 → True（本线程提交者）；
        已被认领 / 已推进（终态）→ False（调用方幂等重放，不重复提交外部，保留 terminal_locked）。
        """
        now = now if now is not None else time.time()
        with self._tx(tenant_id, write=True) as conn:
            row = conn.execute(
                text("SELECT status FROM executions "
                     "WHERE execution_id=:eid AND tenant_id=:t FOR UPDATE"),
                {"eid": execution_id, "t": tenant_id},
            ).mappings().first()
            if row is None:
                raise DomainError(ErrorCode.NOT_FOUND, "执行记录不存在", 404)
            if row["status"] != ExecutionStatus.PENDING_SUBMIT.value:
                return False  # 已推进/终态：不认领（保留终态封闭）。
            res = conn.execute(
                text("UPDATE executions SET attempts=1, updated_at=:now "
                     "WHERE execution_id=:eid AND tenant_id=:t AND status='pending_submit' AND attempts=0"),
                {"now": now, "eid": execution_id, "t": tenant_id},
            )
            return res.rowcount == 1

    def get_execution_record(self, tenant_id: str, execution_id: str) -> ExecutionRecord:
        with self._tx(tenant_id) as conn:
            row = conn.execute(
                text("SELECT * FROM executions WHERE execution_id=:eid AND tenant_id=:t"),
                {"eid": execution_id, "t": tenant_id},
            ).mappings().first()
        if row is None:
            raise DomainError(ErrorCode.NOT_FOUND, "执行记录不存在", 404)
        return self._row_to_execution(row)

    def get_execution_by_operation(self, tenant_id: str, operation_id: str) -> ExecutionRecord | None:
        with self._tx(tenant_id) as conn:
            row = conn.execute(
                text("SELECT * FROM executions WHERE tenant_id=:t AND operation_id=:oid"),
                {"t": tenant_id, "oid": operation_id},
            ).mappings().first()
        return self._row_to_execution(row) if row else None

    def list_execution_records(self, tenant_id: str,
                               status: ExecutionStatus | None = None) -> list[ExecutionRecord]:
        with self._tx(tenant_id) as conn:
            if status is None:
                rows = conn.execute(
                    text("SELECT * FROM executions WHERE tenant_id=:t"), {"t": tenant_id}
                ).mappings().all()
            else:
                rows = conn.execute(
                    text("SELECT * FROM executions WHERE tenant_id=:t AND status=:s"),
                    {"t": tenant_id, "s": status.value},
                ).mappings().all()
        return [self._row_to_execution(r) for r in rows]

    def update_execution_record(self, tenant_id: str, execution_id: str,
                                **fields) -> ExecutionRecord:
        expected_status = fields.pop("expected_status", None)
        cols, args = [], {}
        if expected_status is not None:
            expected_status = (expected_status.value
                               if isinstance(expected_status, ExecutionStatus) else expected_status)
            args["expected_status"] = expected_status
        for k, v in fields.items():
            if k in ("status", "mode") and isinstance(v, (ExecutionStatus, ExecutionMode)):
                v = v.value
            if k in ("receipt", "compensation_result") and v is not None:
                v = json.dumps(v, ensure_ascii=False)
            cols.append(f"{k}=:{k}")
            args[k] = v
        args["eid"] = execution_id
        args["t"] = tenant_id
        # optional CAS：expected_status 提供时仅当当前 status 相符才更新（WHERE status=:expected_status），
        # 否则已被并发推进 → 返回 None（幂等重放，不抛 submitted->submitted 之类非法跃迁）。
        where = "execution_id=:eid AND tenant_id=:t"
        if expected_status is not None:
            where += " AND status=:expected_status"
        with self._tx(tenant_id, write=True) as conn:
            res = conn.execute(
                text(f"UPDATE executions SET {', '.join(cols)} WHERE {where}"),
                args,
            )
            if res.rowcount == 0:
                if expected_status is not None:
                    return None
                raise DomainError(ErrorCode.NOT_FOUND, "执行记录不存在", 404)
        return self.get_execution_record(tenant_id, execution_id)

    def claim_callback(self, tenant_id: str, execution_id: str,
                       nonce: str) -> tuple[ExecutionRecord, bool]:
        """回调非重放窗口的记账（CAS）：首次记账，同 nonce 重放，异 nonce 记为新回调。"""
        with self._tx(tenant_id, write=True) as conn:
            row = conn.execute(
                text("SELECT callback_nonce FROM executions WHERE execution_id=:eid AND tenant_id=:t"),
                {"eid": execution_id, "t": tenant_id},
            ).mappings().first()
            if row is None:
                raise DomainError(ErrorCode.NOT_FOUND, "执行记录不存在", 404)
            if row["callback_nonce"] is None:
                conn.execute(
                    text("UPDATE executions SET callback_nonce=:n, updated_at=:ua "
                         "WHERE execution_id=:eid AND tenant_id=:t AND callback_nonce IS NULL"),
                    {"n": nonce, "ua": time.time(), "eid": execution_id, "t": tenant_id},
                )
                return self.get_execution_record(tenant_id, execution_id), False
            if row["callback_nonce"] == nonce:
                return self.get_execution_record(tenant_id, execution_id), True
            conn.execute(
                text("UPDATE executions SET callback_nonce=:n, updated_at=:ua "
                     "WHERE execution_id=:eid AND tenant_id=:t"),
                {"n": nonce, "ua": time.time(), "eid": execution_id, "t": tenant_id},
            )
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
            receipt=_as_json(row["receipt"]),
            compensation_status=row["compensation_status"],
            compensation_result=_as_json(row["compensation_result"]),
            last_error=row["last_error"], attempts=row["attempts"],
        )

    def apply_callback_atomic(self, tenant_id: str, *, execution_id: str, nonce: str,
                              payload: dict, now: float) -> CallbackAtomicOutcome:
        """回调原子应用（单事务）：非重放窗口记账 + 状态 CAS 终态封闭 + 操作更新 + 审计写入。

        在同一事务内、同一连接上完成：
        a) `FOR UPDATE` 锁定执行行 + `app.tenant_id`（RLS）强制租户隔离；跨租户/不存在 → not_found
           语义（绝不泄露存在）；
        b) nonce 记账：首次记账；同 nonce → 重放（只重放不重复生效）；异 nonce 且非终态 → 允许；
        c) 状态 CAS：`status=:expected` + 非终态 WHERE 子句，RETURNING 确认，仅允许预期态跃迁，
           终态（confirmed/compensated/mismatched/human_handoff/...）不可被任何写覆盖；
        d) 同一事务 UPDATE operations；
        e) 同一事务写审计，**使用执行记录的可信 tenant_id**，user_id="callback"。
        任一环节失败整体回滚，绝不分步提交造成状态不一致。
        """
        from src.infrastructure.store import plan_callback_atomic
        with self._tx(tenant_id, write=True) as conn:
            # a) 定位执行记录（RLS + tenant_id 作用域；跨租户 → 零行 → not_found）。
            row = conn.execute(
                text("SELECT * FROM executions WHERE execution_id=:eid AND tenant_id=:t FOR UPDATE"),
                {"eid": execution_id, "t": tenant_id},
            ).mappings().first()
            if row is None:
                return CallbackAtomicOutcome(applied=False, reason="not_found",
                                             execution_id=execution_id, claimed_nonce=None)
            record = self._row_to_execution(row)
            plan = plan_callback_atomic(record, nonce=nonce, payload=payload, now=now)

            # b/c) 状态 CAS：同一 UPDATE 完成「nonce 记账 + 状态跃迁」，终态封闭不可覆盖。
            if plan["next_status"] is not None:
                sets = ["status=:status", "callback_nonce=:nonce", "updated_at=:ua"]
                params: dict = {"status": plan["next_status"].value, "nonce": nonce, "ua": now,
                                "eid": execution_id, "t": tenant_id,
                                "expected": plan["expected_status"].value}
                for k, v in plan["update_fields"].items():
                    key = f"f_{k}"
                    if k == "receipt" and v is not None:
                        v = json.dumps(v, ensure_ascii=False)
                    sets.append(f"{k}=:{key}")
                    params[key] = v
                not_in = ",".join(f":term_{i}" for i in range(len(_TERMINAL_EXECUTION_STATUSES)))
                for i, ts in enumerate(_TERMINAL_EXECUTION_STATUSES):
                    params[f"term_{i}"] = ts
                res = conn.execute(
                    text(f"UPDATE executions SET {', '.join(sets)} "
                         "WHERE execution_id=:eid AND tenant_id=:t AND status=:expected "
                         f"AND status NOT IN ({not_in})"),
                    params,
                )
                if res.rowcount == 0:
                    # CAS 未命中：并发/已被改写 → 终态封闭语义（本事务无写，空提交）。
                    return CallbackAtomicOutcome(applied=False, reason="terminal_locked",
                                                 execution_id=execution_id, status=plan["status"],
                                                 claimed_nonce=plan["claimed_nonce"])
            elif plan["book_nonce"]:
                # processing：仅记账（无状态跃迁），计入对账窗口。
                conn.execute(
                    text("UPDATE executions SET callback_nonce=:nonce, updated_at=:ua "
                         "WHERE execution_id=:eid AND tenant_id=:t"),
                    {"nonce": nonce, "ua": now, "eid": execution_id, "t": tenant_id},
                )

            # d) 同一事务更新操作状态。
            if plan["operation_status"] is not None:
                conn.execute(
                    text("UPDATE operations SET status=:s, result=:r "
                         "WHERE operation_id=:oid AND tenant_id=:t"),
                    {"s": plan["operation_status"].value,
                     "r": json.dumps(plan["operation_result"], ensure_ascii=False)
                     if plan["operation_result"] else None,
                     "oid": plan["operation_id"], "t": tenant_id},
                )
            # e) 同一事务写权威回调审计（仅在状态跃迁时落）：使用执行记录的可信 tenant_id，
            #    绝不使用请求体不可信 tenant_id。重放/终态封闭/中间态无状态跃迁，不产生额外审计。
            if plan["next_status"] is not None:
                self._append_audit_on(conn, record.tenant_id, "callback", plan["audit_action"],
                                      "execution", execution_id, plan["audit_detail"], now)

            return CallbackAtomicOutcome(applied=plan["applied"], reason=plan["reason"],
                                         execution_id=execution_id, status=plan["status"],
                                         receipt=plan["receipt"],
                                         claimed_nonce=plan["claimed_nonce"])

    # ---- SSE 流 ----
    def create_stream(self, stream_id: str, tenant_id: str, user_id: str, thread_id: str,
                      client_request_id: str, mode: str, now: float) -> None:
        with self._tx(tenant_id, write=True) as conn:
            conn.execute(
                text("INSERT INTO streams(stream_id,tenant_id,user_id,thread_id,client_request_id,"
                     "mode,created_at,last_seq,expires_at) VALUES(:sid,:t,:u,:th,:cr,:m,:ca,0,:ea)"),
                {"sid": stream_id, "t": tenant_id, "u": user_id, "th": thread_id,
                 "cr": client_request_id, "m": mode, "ca": now, "ea": now + 7 * 86400},
            )

    def find_stream_by_client(self, tenant_id: str, user_id: str, client_request_id: str) -> Optional[str]:
        with self._tx(tenant_id) as conn:
            row = conn.execute(
                text("SELECT stream_id FROM streams WHERE tenant_id=:t AND user_id=:u AND client_request_id=:cr"),
                {"t": tenant_id, "u": user_id, "cr": client_request_id},
            ).mappings().first()
        return row["stream_id"] if row else None

    def get_stream(self, tenant_id: str, stream_id: str) -> dict:
        with self._tx(tenant_id) as conn:
            row = conn.execute(
                text("SELECT * FROM streams WHERE stream_id=:sid AND tenant_id=:t"),
                {"sid": stream_id, "t": tenant_id},
            ).mappings().first()
        if row is None:
            raise DomainError(ErrorCode.NOT_FOUND, "流不存在", 404)
        return dict(row)

    def append_event(self, tenant_id: str, stream_id: str, event: str, data: dict, now: float) -> int:
        with self._tx(tenant_id, write=True) as conn:
            # 原子推进 last_seq 并取回新值（同一事务，同连接）。
            seq_row = conn.execute(
                text("UPDATE streams SET last_seq=last_seq+1 WHERE stream_id=:sid AND tenant_id=:t "
                     "RETURNING last_seq"),
                {"sid": stream_id, "t": tenant_id},
            ).mappings().first()
            if seq_row is None:
                raise DomainError(ErrorCode.NOT_FOUND, "流不存在", 404)
            seq = int(seq_row["last_seq"])
            conn.execute(
                text("INSERT INTO stream_events(stream_id,seq,event,data,created_at,tenant_id) "
                     "VALUES(:sid,:seq,:ev,:data,:ca,:t)"),
                {"sid": stream_id, "seq": seq, "ev": event,
                 "data": json.dumps(data, ensure_ascii=False), "ca": now, "t": tenant_id},
            )
        return seq

    def events_after(self, tenant_id: str, stream_id: str, last_seq: int) -> list[dict]:
        # 先校验流归属（跨租户 404），再读取事件。
        self.get_stream(tenant_id, stream_id)
        with self._tx(tenant_id) as conn:
            rows = conn.execute(
                text("SELECT * FROM stream_events WHERE stream_id=:sid AND seq>:ls ORDER BY seq"),
                {"sid": stream_id, "ls": last_seq},
            ).mappings().all()
        return [{"stream_id": r["stream_id"], "seq": r["seq"], "event": r["event"],
                 "data": _as_json(r["data"]), "created_at": r["created_at"]} for r in rows]

    # ---- 审计 ----
    def append_audit(self, tenant_id: str, user_id: str, action: str, target_type: str,
                     target_id: str, detail: dict, now: float) -> AuditRecord:
        import uuid
        audit_id = str(uuid.uuid4())
        with self._tx(tenant_id, write=True) as conn:
            conn.execute(
                text("INSERT INTO audit(audit_id,tenant_id,user_id,action,target_type,target_id,detail,created_at) "
                     "VALUES(:aid,:t,:u,:act,:tt,:tid,:d,:ca)"),
                {"aid": audit_id, "t": tenant_id, "u": user_id, "act": action, "tt": target_type,
                 "tid": target_id, "d": json.dumps(detail, ensure_ascii=False), "ca": now},
            )
        return AuditRecord(audit_id=audit_id, tenant_id=tenant_id, user_id=user_id, action=action,
                           target_type=target_type, target_id=target_id, detail=detail, created_at=now)

    def list_audit(self, tenant_id: str) -> list[AuditRecord]:
        with self._tx(tenant_id) as conn:
            rows = conn.execute(
                text("SELECT * FROM audit WHERE tenant_id=:t"), {"t": tenant_id}
            ).mappings().all()
        return [AuditRecord(audit_id=r["audit_id"], tenant_id=r["tenant_id"], user_id=r["user_id"],
                            action=r["action"], target_type=r["target_type"], target_id=r["target_id"],
                            detail=_as_json(r["detail"]), created_at=r["created_at"]) for r in rows]

    def search_audit(self, tenant_id: str, **filters) -> list[AuditRecord]:
        from src.infrastructure.store import filter_audit
        return filter_audit(self.list_audit(tenant_id), **filters)


    # ---- checkpoint scope（应用层权威映射：滑动 7 天 TTL + 删除清理）----
    def create_checkpoint_scope(self, tenant_id: str, thread_id: str, now: float, ttl_days: int) -> None:
        expires_at = now + ttl_days * 86400
        with self._tx(tenant_id, write=True) as conn:
            conn.execute(
                text("INSERT INTO checkpoint_thread_scopes(tenant_id,thread_id,created_at,last_active_at,"
                     "expires_at,status,cleanup_attempts) VALUES(:t,:th,:ca,:la,:ea,'active',0) "
                     "ON CONFLICT (thread_id) DO NOTHING"),
                {"t": tenant_id, "th": thread_id, "ca": now, "la": now, "ea": expires_at},
            )

    def get_checkpoint_scope(self, tenant_id: str, thread_id: str) -> Optional[dict]:
        with self._tx(tenant_id) as conn:
            row = conn.execute(
                text("SELECT * FROM checkpoint_thread_scopes WHERE tenant_id=:t AND thread_id=:th"),
                {"t": tenant_id, "th": thread_id},
            ).mappings().first()
        return dict(row) if row else None

    def touch_checkpoint_scope(self, tenant_id: str, thread_id: str, now: float, ttl_days: int) -> None:
        expires_at = now + ttl_days * 86400
        with self._tx(tenant_id, write=True) as conn:
            conn.execute(
                text("UPDATE checkpoint_thread_scopes SET last_active_at=:la, expires_at=:ea "
                     "WHERE tenant_id=:t AND thread_id=:th AND status='active'"),
                {"la": now, "ea": expires_at, "t": tenant_id, "th": thread_id},
            )

    def mark_scope_deleting(self, tenant_id: str, thread_id: str) -> None:
        with self._tx(tenant_id, write=True) as conn:
            conn.execute(
                text("UPDATE checkpoint_thread_scopes SET status='deleting' "
                     "WHERE tenant_id=:t AND thread_id=:th"),
                {"t": tenant_id, "th": thread_id},
            )

    def claim_expired_scopes_for_cleanup(self, now: float, batch_size: int = 100,
                                         engine: Engine | None = None) -> list[dict]:
        """占用（claim）已过期且 active 的 scope：标记为 deleting 并返回候选。

        使用 FOR UPDATE SKIP LOCKED + 原子 UPDATE，保证多 worker 并发清理时不重复领取；
        标记 deleting 会阻断新的图执行（授权校验要求 status='active'）。

        注意：这是**系统级跨租户扫描**（scope 表启用 RLS 后，普通运行角色未设置
        app.tenant_id 时会零可见）。因此清理须传入可绕过 RLS 的系统引擎
        （超级用户 / BYPASSRLS 运维角色），符合"迁移/运行角色分离"；每个候选的具体
        checkpoint 删除与 scope/session 删除仍走 `app.tenant_id` 作用域。
        """
        eng = engine or self._engine
        with eng.begin() as conn:
            rows = conn.execute(
                text("UPDATE checkpoint_thread_scopes s "
                     "SET status='deleting', cleanup_attempts=s.cleanup_attempts+1 "
                     "WHERE s.expires_at < :now AND s.status='active' AND s.thread_id IN ("
                     "  SELECT c.thread_id FROM checkpoint_thread_scopes c "
                     "  WHERE c.expires_at < :now AND c.status='active' "
                     "  ORDER BY c.expires_at LIMIT :batch FOR UPDATE SKIP LOCKED) "
                     "RETURNING s.tenant_id, s.thread_id, s.expires_at, s.cleanup_attempts"),
                {"now": now, "batch": batch_size},
            ).mappings().all()
        return [dict(r) for r in rows]

    def finish_cleanup_scope(self, tenant_id: str, thread_id: str, now: float) -> None:
        """删除 scope + session（同事务）；业务表经 FK ON DELETE CASCADE 级联清理。"""
        with self._tx(tenant_id, write=True) as conn:
            conn.execute(
                text("DELETE FROM sessions WHERE tenant_id=:t AND thread_id=:th"),
                {"t": tenant_id, "th": thread_id},
            )
            conn.execute(
                text("DELETE FROM checkpoint_thread_scopes WHERE tenant_id=:t AND thread_id=:th"),
                {"t": tenant_id, "th": thread_id},
            )
            self._append_audit_on(conn, tenant_id, "system", "cleanup.scope", "session", thread_id,
                                  {"thread_id": thread_id, "phase": "finished"}, now)

    def record_cleanup_failure(self, tenant_id: str, thread_id: str, err: object, now: float) -> None:
        """清理失败：保留 deleting scope、再递增尝试、写审计；下一轮幂等重试。"""
        with self._tx(tenant_id, write=True) as conn:
            conn.execute(
                text("UPDATE checkpoint_thread_scopes SET cleanup_attempts=cleanup_attempts+1 "
                     "WHERE tenant_id=:t AND thread_id=:th"),
                {"t": tenant_id, "th": thread_id},
            )
            self._append_audit_on(conn, tenant_id, "system", "cleanup.failed", "session", thread_id,
                                  {"thread_id": thread_id, "error": str(err)[:500]}, now)

    @staticmethod
    def _append_audit_on(conn, tenant_id: str, user_id: str, action: str, target_type: str,
                         target_id: str, detail: dict, now: float) -> None:
        import uuid
        conn.execute(
            text("INSERT INTO audit(audit_id,tenant_id,user_id,action,target_type,target_id,detail,created_at) "
                 "VALUES(:aid,:t,:u,:act,:tt,:tid,:d,:ca)"),
            {"aid": str(uuid.uuid4()), "t": tenant_id, "u": user_id, "act": action,
             "tt": target_type, "tid": target_id,
             "d": json.dumps(detail, ensure_ascii=False), "ca": now},
        )

    # ---- 会话 + scope 授权（checkpointer 入口的归属校验）----
    def authorize_checkpoint_access(self, tenant_id: str, user_id: str, role: str,
                                    thread_id: str, now: float, ttl_days: int,
                                    source: str = "request") -> dict:
        """校验 session 归属 + scope 状态，并在同一事务内滑动续期 TTL；失败默认拒绝。

        返回构造 CheckpointRequestScope 所需的不变字段（tenant_id/user_id/thread_id/source）。
        校验通过后才由调用方写入可信 ContextVar；绝不能把请求端的 tenant_id 作授权依据。
        """
        session = self.get_session(tenant_id, thread_id)  # 跨租户 → 404（不泄露存在）
        # customer 仅能访问本人会话；agent/admin/approver 按租户 RBAC。
        if session.user_id != user_id and role not in {"agent", "admin", "approver"}:
            raise DomainError(ErrorCode.FORBIDDEN, "无权访问该会话", 403)
        if session.status != SessionStatus.ACTIVE:
            raise DomainError(ErrorCode.FORBIDDEN, "会话不可用", 403)
        # scope 校验：必须存在且 active（deleting 时拒绝恢复/续跑）。
        scope = self.get_checkpoint_scope(tenant_id, thread_id)
        if scope is None or scope["status"] != "active":
            raise DomainError(ErrorCode.FORBIDDEN, "检查点作用域不可用", 403)
        # 滑动 7 天 TTL：session + scope 必须在同一受控事务内更新。
        expires_at = now + ttl_days * 86400
        with self._tx(tenant_id, write=True) as conn:
            conn.execute(
                text("UPDATE sessions SET last_active_at=:la, expires_at=:ea "
                     "WHERE tenant_id=:t AND thread_id=:th"),
                {"la": now, "ea": expires_at, "t": tenant_id, "th": thread_id},
            )
            conn.execute(
                text("UPDATE checkpoint_thread_scopes SET last_active_at=:la, expires_at=:ea "
                     "WHERE tenant_id=:t AND thread_id=:th AND status='active'"),
                {"la": now, "ea": expires_at, "t": tenant_id, "th": thread_id},
            )
        return {"tenant_id": tenant_id, "user_id": user_id, "thread_id": thread_id, "source": source}


def _to_sqlalchemy_url(dsn: str) -> str:
    """把裸 postgresql:// 规范化成 SQLAlchemy 的 psycopg3 驱动 URL。"""
    if "+psycopg" in dsn or "+psycopg2" in dsn or "+asyncpg" in dsn:
        return dsn
    if dsn.startswith("postgresql://"):
        return dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    if dsn.startswith("postgres://"):
        return dsn.replace("postgres://", "postgresql+psycopg://", 1)
    return dsn
