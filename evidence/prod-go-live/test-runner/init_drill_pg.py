"""初始化一次性独立 Postgres（DR/恢复演练数据面），并修复仓库迁移中的 shipping_events FK 缺陷。

背景：
- 仓库 `src/infrastructure/migrations.py` 的 BUSINESS_SCHEMA_SQL 中 shipping_events 的 FK
  写成 `order_id TEXT NOT NULL REFERENCES orders(tenant_id, order_id)` —— 单个引用列却指向
  两列复合主键（`number of referencing and referenced columns for foreign key disagree`），
  导致任何对干净库调用 `initialize_all()` 都会在 apply_business_schema 阶段失败。
- 这是**生产阻断级迁移缺陷**（fresh prod DB 无法初始化）。本脚本为演练数据面**仅修正这一条
  DDL**（改为复合 FK `FOREIGN KEY (tenant_id, order_id) REFERENCES orders(tenant_id, order_id)`），
  其余全部复用仓库真实的迁移语句，以保持与真实 schema/RLS 一致。
- 同时 seed 租户 TENANT-A/TENANT-B 样本（含敏感审批终态 + 执行记录 + 审计），并创建
  `app_runtime`（运行角色，NOBYPASSRLS）与 `backup_role`（最小权限只读，BYPASSRLS）供备份脚本使用。

连接：migrator（owner/SUPERUSER）直连一次性的独立库。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import src.observability.metrics  # noqa: E402, F401
from sqlalchemy import create_engine, text  # noqa: E402

from src.core.types import PendingAction, Role  # noqa: E402
from src.execution import ExecutionMode, ExecutionStatus  # noqa: E402
from src.infrastructure import migrations  # noqa: E402
from src.infrastructure.postgres_store import _to_sqlalchemy_url  # noqa: E402


def patched_schema_sql() -> list[str]:
    """返回仓库真实 BUSINESS_SCHEMA_SQL，但把 shipping_events 的 FK 修正为复合外键。"""
    out = []
    for stmt in migrations.BUSINESS_SCHEMA_SQL:
        if "CREATE TABLE IF NOT EXISTS shipping_events" in stmt:
            out.append(
                """
                CREATE TABLE IF NOT EXISTS shipping_events (
                    tenant_id TEXT NOT NULL REFERENCES tenants(id),
                    order_id TEXT NOT NULL,
                    tracking_no TEXT,
                    events JSONB NOT NULL DEFAULT '[]',
                    PRIMARY KEY (tenant_id, order_id),
                    FOREIGN KEY (tenant_id, order_id) REFERENCES orders(tenant_id, order_id) ON DELETE CASCADE
                )
                """
            )
        else:
            out.append(stmt)
    return out


def init(engine) -> None:
    with engine.begin() as conn:
        for stmt in patched_schema_sql():
            conn.execute(text(stmt))
        for stmt in migrations.SCOPE_FUNCTIONS_SQL:
            conn.execute(text(stmt))
        for stmt in migrations.RLS_TABLES_SQL:
            conn.execute(text(stmt))
    migrations.initialize_checkpoint_schema(engine)
    migrations.apply_checkpoint_rls(engine)
    migrations.apply_backup_role(engine, password="drill_backup_pw_QWE456", dbname="langgraph_drill")
    migrations.apply_runtime_role(engine, password="drill_app_runtime_pw_ABC789")


def seed(engine) -> None:
    now = time.time()
    with engine.begin() as conn:
        def ins(sql, **kw):
            conn.execute(text(sql).bindparams(**{k: v for k, v in kw.items() if v is not None}))

        for t in ("TENANT-A", "TENANT-B"):
            ins("INSERT INTO tenants(id,name,status,created_at) VALUES (:id,:name,'active',:c) ON CONFLICT (id) DO NOTHING",
                id=t, name=f"租户-{t}", c=now)
        ins("INSERT INTO memberships(tenant_id,user_id,role,status) VALUES ('TENANT-A','USER-A','customer','active') ON CONFLICT DO NOTHING")
        ins("INSERT INTO memberships(tenant_id,user_id,role,status) VALUES ('TENANT-B','USER-B','customer','active') ON CONFLICT DO NOTHING")
        ins("INSERT INTO sessions(tenant_id,thread_id,user_id,created_at,expires_at,last_active_at,status,message_count) VALUES ('TENANT-A','th-a','USER-A',:c,:e,:c,'active',0) ON CONFLICT DO NOTHING",
            c=now, e=now + 7 * 86400)
        ins("INSERT INTO sessions(tenant_id,thread_id,user_id,created_at,expires_at,last_active_at,status,message_count) VALUES ('TENANT-B','th-b','USER-B',:c,:e,:c,'active',0) ON CONFLICT DO NOTHING",
            c=now, e=now + 7 * 86400)

        # operations：同租户同幂等键唯一；跨租户同键互不覆盖；另加一个审批终态样本。
        ins("INSERT INTO operations(operation_id,tenant_id,thread_id,order_id,pending_action,idempotency_key,status,created_at) VALUES ('op-refund-a','TENANT-A','th-a','ORD-A','refund','oprefund:TENANT-A:ORD-A:R1','executed',:c)", c=now)
        ins("INSERT INTO operations(operation_id,tenant_id,thread_id,order_id,pending_action,idempotency_key,status,created_at) VALUES ('op-return-b','TENANT-B','th-b','ORD-B','return_request','opreturn:TENANT-B:ORD-B:R1','executed',:c)", c=now)
        ins("INSERT INTO operations(operation_id,tenant_id,thread_id,order_id,pending_action,idempotency_key,status,created_at) VALUES ('op-pending-a','TENANT-A','th-a','ORD-A','refund','oprefund:TENANT-A:ORD-A:R2','pending',:c)", c=now)

        # approvals：唯一（一个操作至多一个审批单），TENANT-A 一个 approved 终态。
        ins("INSERT INTO approvals(approval_id,tenant_id,thread_id,operation_id,pending_action,status,created_at,order_id,amount,reason,approver,decided_at) VALUES ('appr-1','TENANT-A','th-a','op-refund-a','refund','approved',:c,'ORD-A',100.0,'退款','ADMIN-A',:c)", c=now)

        # executions：每操作至多一条（幂等锚点）。
        ins("INSERT INTO executions(execution_id,tenant_id,operation_id,pending_action,order_id,idempotency_key,mode,status,created_at,updated_at,amount,external_txn_id,callback_nonce,confirmed_at) VALUES ('exec-a','TENANT-A','op-refund-a','refund','ORD-A','oprefund:TENANT-A:ORD-A:R1','live','confirmed',:c,:c,100.0,'txn-EXT-A','nonce-a',:c)", c=now)
        ins("INSERT INTO executions(execution_id,tenant_id,operation_id,pending_action,order_id,idempotency_key,mode,status,created_at,updated_at,amount,external_txn_id,submitted_at) VALUES ('exec-b','TENANT-B','op-return-b','return_request','ORD-B','opreturn:TENANT-B:ORD-B:R1','live','submitted',:c,:c,50.0,'txn-EXT-B',:c)", c=now)

        # audit：可追溯 + tenant 归属。
        ins("INSERT INTO audit(audit_id,tenant_id,user_id,action,target_type,target_id,detail,created_at) VALUES ('audit-1','TENANT-A','USER-A','execution.confirm','execution','exec-a',cast(:d AS jsonb),:c)", d='{"operation_id":"op-refund-a","amount":100.0}', c=now)
        ins("INSERT INTO audit(audit_id,tenant_id,user_id,action,target_type,target_id,detail,created_at) VALUES ('audit-2','TENANT-A','system','execution.compensated','execution','exec-b',cast(:d AS jsonb),:c)", d='{"operation_id":"op-return-b","reason":"dispatch_failure_compensated"}', c=now)

        # orders（供 shipping_events FK 指向）+ 一条 shipping_event 样本。
        ins("INSERT INTO orders(tenant_id,order_id,user_id,status,total_amount,created_at) VALUES ('TENANT-A','ORD-A','USER-A','delivered',100.0,:c) ON CONFLICT (tenant_id,order_id) DO NOTHING", c=now)
        ins("INSERT INTO orders(tenant_id,order_id,user_id,status,total_amount,created_at) VALUES ('TENANT-B','ORD-B','USER-B','delivered',50.0,:c) ON CONFLICT (tenant_id,order_id) DO NOTHING", c=now)
        ins("INSERT INTO shipping_events(tenant_id,order_id,tracking_no,events) VALUES ('TENANT-A','ORD-A','TRK-A',cast(:e AS jsonb)) ON CONFLICT (tenant_id,order_id) DO NOTHING", e='[]')
        ins("INSERT INTO shipping_events(tenant_id,order_id,tracking_no,events) VALUES ('TENANT-B','ORD-B','TRK-B',cast(:e AS jsonb)) ON CONFLICT (tenant_id,order_id) DO NOTHING", e='[]')


def main() -> None:
    dsn = sys.argv[1] if len(sys.argv) > 1 else "postgresql://migrator:drill_super_pw_ZXC123@127.0.0.1:56742/langgraph_drill"
    schema_only = "--schema-only" in sys.argv
    eng = create_engine(_to_sqlalchemy_url(dsn), pool_pre_ping=True)
    init(eng)
    if schema_only:
        print("DRILL_PG_INIT_OK schema-only applied (shipping_events FK patched).")
    else:
        seed(eng)
        print("DRILL_PG_INIT_OK schema+seed applied (shipping_events FK patched).")
    eng.dispose()


if __name__ == "__main__":
    main()
