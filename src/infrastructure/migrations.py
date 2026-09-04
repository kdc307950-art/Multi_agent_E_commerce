"""PostgreSQL 数据面迁移（Phase 2 多租户/RLS）。

设计依据：《生产基线与验收测试》§四、《会话与线程管理设计文档》§三.3/§五.2。

要点：
- 业务表带 `tenant_id` 外键 + 状态/角色 CHECK + 跨租户联合唯一约束。
- `tenants` 是租户主数据，由平台/迁移角色维护，**不启用 RLS**；其余业务表全部
  `ENABLE + FORCE ROW LEVEL SECURITY`，policy 用 `app_current_tenant_id()` 做租户作用域。
- 官方 checkpoint 表（checkpoints/checkpoint_blobs/checkpoint_writes）由
  `AsyncPostgresSaver.setup()` 建表；本模块在 `apply_checkpoint_rls` 里对它们启用 RLS，
  policy 通过 `checkpoint_thread_in_current_tenant(thread_id)` 与
  `checkpoint_thread_scopes` 做存在性租户关联（官方表只带 thread_id，不重复加 tenant_id 列）。
- 迁移/运行角色分离：建议 `app_runtime`（LOGIN NOINHERIT NOSUPERUSER NOBYPASSRLS）作为
  应用运行角色；`FORCE RLS` 保证即便是表 owner 也按 policy 过滤。GRANT 交由部署层执行，
  见 configure_app_runtime 说明。

时间戳约定：与现有领域对象保持一致，统一使用 DOUBLE PRECISION 存 Unix 秒（time.time()）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

# ---------------------------------------------------------------------------
# 业务 schema（按依赖顺序，一行一个可独立执行的语句）
# ---------------------------------------------------------------------------
BUSINESS_SCHEMA_SQL: list[str] = [
    # tenants：租户主数据（平台层，不启用 RLS）
    """
    CREATE TABLE IF NOT EXISTS tenants (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('active', 'suspended', 'deleting')),
        created_at DOUBLE PRECISION NOT NULL
    )
    """,
    # memberships：租户成员关系（角色仅限四类租户角色，platform_admin 不在此处）
    """
    CREATE TABLE IF NOT EXISTS memberships (
        tenant_id TEXT NOT NULL REFERENCES tenants(id),
        user_id TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('customer', 'agent', 'admin', 'approver')),
        status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
        PRIMARY KEY (tenant_id, user_id)
    )
    """,
    # sessions：会话归属（租户 + 线程联合主键，滑动 7 天 TTL 由应用层维护）
    """
    CREATE TABLE IF NOT EXISTS sessions (
        tenant_id TEXT NOT NULL REFERENCES tenants(id),
        thread_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        created_at DOUBLE PRECISION NOT NULL,
        expires_at DOUBLE PRECISION NOT NULL,
        last_active_at DOUBLE PRECISION NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('active', 'expired', 'deleting')),
        message_count INTEGER NOT NULL DEFAULT 0,
        title TEXT,
        PRIMARY KEY (tenant_id, thread_id)
    )
    """,
    # operations：业务操作（跨租户幂等唯一：tenant_id + idempotency_key）
    """
    CREATE TABLE IF NOT EXISTS operations (
        operation_id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenants(id),
        thread_id TEXT NOT NULL,
        order_id TEXT NOT NULL,
        pending_action TEXT NOT NULL
            CHECK (pending_action IN ('refund', 'return_request', 'return_address', 'policy', 'other')),
        idempotency_key TEXT NOT NULL,
        status TEXT NOT NULL
            CHECK (status IN ('pending', 'executed', 'failed', 'rejected', 'human_handoff')),
        created_at DOUBLE PRECISION NOT NULL,
        result JSONB,
        UNIQUE (tenant_id, idempotency_key),
        FOREIGN KEY (tenant_id, thread_id) REFERENCES sessions (tenant_id, thread_id) ON DELETE CASCADE
    )
    """,
    # approvals：审批单（一个操作在租户内至多一个审批单；归属会话/操作）
    """
    CREATE TABLE IF NOT EXISTS approvals (
        approval_id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenants(id),
        thread_id TEXT NOT NULL,
        operation_id TEXT NOT NULL REFERENCES operations(operation_id) ON DELETE CASCADE,
        pending_action TEXT NOT NULL
            CHECK (pending_action IN ('refund', 'return_request', 'return_address', 'policy', 'other')),
        status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'timeout')),
        created_at DOUBLE PRECISION NOT NULL,
        order_id TEXT,
        amount DOUBLE PRECISION,
        reason TEXT,
        approver TEXT,
        feedback TEXT,
        decided_at DOUBLE PRECISION,
        FOREIGN KEY (tenant_id, thread_id) REFERENCES sessions (tenant_id, thread_id) ON DELETE CASCADE,
        UNIQUE (tenant_id, operation_id)
    )
    """,
    # executions：执行记录（资金/业务执行面）。幂等锚点 = operation_id（每操作至多一条执行）。
    # 执行状态机见 src/execution/types.py 的 ExecutionStatus；mode 为 shadow/live（受控开关）。
    """
    CREATE TABLE IF NOT EXISTS executions (
        execution_id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenants(id),
        operation_id TEXT NOT NULL REFERENCES operations(operation_id) ON DELETE CASCADE,
        pending_action TEXT NOT NULL
            CHECK (pending_action IN ('refund', 'return_request', 'return_address', 'policy', 'other')),
        order_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        mode TEXT NOT NULL CHECK (mode IN ('shadow', 'live')),
        status TEXT NOT NULL
            CHECK (status IN ('pending_submit', 'submitted', 'confirmed', 'failed_dispatch',
                              'failed_uncertain', 'compensating', 'compensated',
                              'compensation_failed', 'reconciling', 'reconciled',
                              'mismatched', 'human_handoff')),
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL,
        amount DOUBLE PRECISION,
        external_txn_id TEXT,
        callback_nonce TEXT,
        submitted_at DOUBLE PRECISION,
        confirmed_at DOUBLE PRECISION,
        receipt JSONB,
        compensation_status TEXT,
        compensation_result JSONB,
        last_error TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        UNIQUE (tenant_id, operation_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_executions_tenant_status
        ON executions (tenant_id, status)
    """,
    # streams：SSE 流（start 原子去重：tenant_id + user_id + client_request_id）
    """
    CREATE TABLE IF NOT EXISTS streams (
        stream_id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenants(id),
        user_id TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        client_request_id TEXT NOT NULL,
        mode TEXT NOT NULL CHECK (mode IN ('start', 'resume')),
        created_at DOUBLE PRECISION NOT NULL,
        last_seq INTEGER NOT NULL DEFAULT 0,
        expires_at DOUBLE PRECISION NOT NULL,
        FOREIGN KEY (tenant_id, thread_id) REFERENCES sessions (tenant_id, thread_id) ON DELETE CASCADE,
        UNIQUE (tenant_id, user_id, client_request_id)
    )
    """,
    # stream_events：冗余 tenant_id 便于直接 RLS 过滤（仍以 stream 外键为主约束）
    """
    CREATE TABLE IF NOT EXISTS stream_events (
        stream_id TEXT NOT NULL REFERENCES streams(stream_id) ON DELETE CASCADE,
        seq INTEGER NOT NULL,
        event TEXT NOT NULL,
        data JSONB NOT NULL,
        created_at DOUBLE PRECISION NOT NULL,
        tenant_id TEXT NOT NULL REFERENCES tenants(id),
        PRIMARY KEY (stream_id, seq)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_stream_events_tenant_seq
        ON stream_events (tenant_id, stream_id, seq)
    """,
    # audit：审计记录（租户 + 时间索引）
    """
    CREATE TABLE IF NOT EXISTS audit (
        audit_id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenants(id),
        user_id TEXT NOT NULL,
        action TEXT NOT NULL,
        target_type TEXT NOT NULL,
        target_id TEXT NOT NULL,
        detail JSONB NOT NULL,
        created_at DOUBLE PRECISION NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_audit_tenant_created ON audit (tenant_id, created_at)
    """,
    # orders：业务订单（受控数据源）。items 为 JSONB（商品明细）；created_at/delivered_at
    # 为 Unix 秒，用于退款/退货资格窗口判定。跨租户读取由 RLS 隔离 + 适配器归属校验兜底。
    """
    CREATE TABLE IF NOT EXISTS orders (
        tenant_id TEXT NOT NULL REFERENCES tenants(id),
        order_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        status TEXT NOT NULL,
        total_amount DOUBLE PRECISION NOT NULL,
        items JSONB NOT NULL DEFAULT '[]',
        carrier TEXT,
        tracking_no TEXT,
        created_at DOUBLE PRECISION,
        delivered_at DOUBLE PRECISION,
        PRIMARY KEY (tenant_id, order_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_orders_tenant_user
        ON orders (tenant_id, user_id)
    """,
    # shipping_events：物流轨迹（每个订单至多一条轨迹，events 为事件 JSONB 数组）
    """
    CREATE TABLE IF NOT EXISTS shipping_events (
        tenant_id TEXT NOT NULL REFERENCES tenants(id),
        order_id TEXT NOT NULL,
        tracking_no TEXT,
        events JSONB NOT NULL DEFAULT '[]',
        PRIMARY KEY (tenant_id, order_id),
        FOREIGN KEY (tenant_id, order_id) REFERENCES orders(tenant_id, order_id) ON DELETE CASCADE
    )
    """,
    # policy_documents：售后政策（租户作用域，供检索后端预载 / 关键字检索）
    """
    CREATE TABLE IF NOT EXISTS policy_documents (
        tenant_id TEXT NOT NULL REFERENCES tenants(id),
        doc_id TEXT NOT NULL,
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        PRIMARY KEY (tenant_id, doc_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_policy_documents_tenant
        ON policy_documents (tenant_id)
    """,
    # checkpoint_thread_scopes：应用层权威 scope 映射（官方 checkpoint 表只带 thread_id，
    # 此处建立 tenant_id + thread_id 合法归属，供 RLS 存在性关联与清理 claim 使用）
    """
    CREATE TABLE IF NOT EXISTS checkpoint_thread_scopes (
        tenant_id TEXT NOT NULL REFERENCES tenants(id),
        thread_id TEXT NOT NULL,
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0,
        last_active_at DOUBLE PRECISION NOT NULL DEFAULT 0,
        expires_at DOUBLE PRECISION NOT NULL,
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'deleting')),
        cleanup_attempts INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (tenant_id, thread_id),
        UNIQUE (thread_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_checkpoint_scope_expiry
        ON checkpoint_thread_scopes (tenant_id, expires_at)
    """,
]

# 需要 RLS 的业务表（tenants 除外；都带 tenant_id 列）
RLS_TABLES_SQL: list[str] = [
    """
    ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;
    ALTER TABLE {t} FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS {t}_tenant_scope ON {t};
    CREATE POLICY {t}_tenant_scope ON {t}
        USING (tenant_id = app_current_tenant_id())
        WITH CHECK (tenant_id = app_current_tenant_id());
    """.format(t="memberships"),
    """
    ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;
    ALTER TABLE {t} FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS {t}_tenant_scope ON {t};
    CREATE POLICY {t}_tenant_scope ON {t}
        USING (tenant_id = app_current_tenant_id())
        WITH CHECK (tenant_id = app_current_tenant_id());
    """.format(t="sessions"),
    """
    ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;
    ALTER TABLE {t} FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS {t}_tenant_scope ON {t};
    CREATE POLICY {t}_tenant_scope ON {t}
        USING (tenant_id = app_current_tenant_id())
        WITH CHECK (tenant_id = app_current_tenant_id());
    """.format(t="operations"),
    """
    ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;
    ALTER TABLE {t} FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS {t}_tenant_scope ON {t};
    CREATE POLICY {t}_tenant_scope ON {t}
        USING (tenant_id = app_current_tenant_id())
        WITH CHECK (tenant_id = app_current_tenant_id());
    """.format(t="approvals"),
    """
    ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;
    ALTER TABLE {t} FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS {t}_tenant_scope ON {t};
    CREATE POLICY {t}_tenant_scope ON {t}
        USING (tenant_id = app_current_tenant_id())
        WITH CHECK (tenant_id = app_current_tenant_id());
    """.format(t="executions"),
    """
    ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;
    ALTER TABLE {t} FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS {t}_tenant_scope ON {t};
    CREATE POLICY {t}_tenant_scope ON {t}
        USING (tenant_id = app_current_tenant_id())
        WITH CHECK (tenant_id = app_current_tenant_id());
    """.format(t="streams"),
    """
    ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;
    ALTER TABLE {t} FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS {t}_tenant_scope ON {t};
    CREATE POLICY {t}_tenant_scope ON {t}
        USING (tenant_id = app_current_tenant_id())
        WITH CHECK (tenant_id = app_current_tenant_id());
    """.format(t="stream_events"),
    """
    ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;
    ALTER TABLE {t} FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS {t}_tenant_scope ON {t};
    CREATE POLICY {t}_tenant_scope ON {t}
        USING (tenant_id = app_current_tenant_id())
        WITH CHECK (tenant_id = app_current_tenant_id());
    """.format(t="audit"),
    """
    ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;
    ALTER TABLE {t} FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS {t}_tenant_scope ON {t};
    CREATE POLICY {t}_tenant_scope ON {t}
        USING (tenant_id = app_current_tenant_id())
        WITH CHECK (tenant_id = app_current_tenant_id());
    """.format(t="checkpoint_thread_scopes"),
    """
    ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;
    ALTER TABLE {t} FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS {t}_tenant_scope ON {t};
    CREATE POLICY {t}_tenant_scope ON {t}
        USING (tenant_id = app_current_tenant_id())
        WITH CHECK (tenant_id = app_current_tenant_id());
    """.format(t="orders"),
    """
    ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;
    ALTER TABLE {t} FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS {t}_tenant_scope ON {t};
    CREATE POLICY {t}_tenant_scope ON {t}
        USING (tenant_id = app_current_tenant_id())
        WITH CHECK (tenant_id = app_current_tenant_id());
    """.format(t="shipping_events"),
    """
    ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;
    ALTER TABLE {t} FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS {t}_tenant_scope ON {t};
    CREATE POLICY {t}_tenant_scope ON {t}
        USING (tenant_id = app_current_tenant_id())
        WITH CHECK (tenant_id = app_current_tenant_id());
    """.format(t="policy_documents"),
]

# 官方 checkpoint 表（由 saver.setup() 建表后启用 RLS；policy 用存在性关联）
CHECKPOINT_RLS_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
CHECKPOINT_RLS_SQL: list[str] = [
    """
    ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;
    ALTER TABLE {t} FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS {t}_tenant_scope ON {t};
    CREATE POLICY {t}_tenant_scope ON {t}
        USING (checkpoint_thread_in_current_tenant(thread_id))
        WITH CHECK (checkpoint_thread_in_current_tenant(thread_id));
    """.format(t=t)
    for t in CHECKPOINT_RLS_TABLES
]

# 租户作用域函数（供 RLS policy 使用）
SCOPE_FUNCTIONS_SQL: list[str] = [
    """
    CREATE OR REPLACE FUNCTION app_current_tenant_id() RETURNS TEXT
    LANGUAGE sql STABLE
    AS $$ SELECT NULLIF(current_setting('app.tenant_id', true), '') $$
    """,
    """
    CREATE OR REPLACE FUNCTION checkpoint_thread_in_current_tenant(candidate_thread_id TEXT)
    RETURNS BOOLEAN
    LANGUAGE sql STABLE
    AS $$
        SELECT EXISTS (
            SELECT 1
            FROM checkpoint_thread_scopes AS s
            WHERE s.thread_id = candidate_thread_id
              AND s.tenant_id = app_current_tenant_id()
        )
    $$
    """,
]

# 建议的运行角色（迁移/运行角色分离；口令由部署层密钥注入，GRANT 由部署层执行）
APP_RUNTIME_ROLE = "app_runtime"
APP_RUNTIME_ROLE_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
        CREATE ROLE {role} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
END
$$;
""".format(role=APP_RUNTIME_ROLE)


# 建议的**最小权限备份角色**（只读，专供备份脚本做 pg_dump 导出）。
# 与运行角色 app_runtime（DML）、迁移角色（owner/DDL）三方分离：
#   - 迁移角色：建表/RLS/函数（owner），具备 CREATE/ALTER；
#   - 运行角色 app_runtime：仅 GRANT 后的 DML（app 数据面）；**NOBYPASSRLS**（租户隔离靠 RLS）；
#   - 备份角色 backup_role：仅 LOGIN + CONNECT + USAGE + SELECT（表/序列），**无任何写/DDL 权限**。
# 备份脚本（backup_encrypted.sh / preview_backup_scheduler.sh）用它做 `pg_dump -Fc`，绝不使用 owner/超级用户。
#
# 【关键设计】备份角色必须 BYPASSRLS：完整备份需要读出**全部行**（含所有租户），而业务表已
# ENABLE/FORCE RLS，非 owner 且 NOBYPASSRLS 的角色执行 `COPY ... TO stdout`（pg_dump 读数据）会被 RLS
# 拦截（"query would be affected by row-level security policy"）。BYPASSRLS 只是**绕开行级过滤以读出全量**，
# 它**不是写/DDL 权限**：备份角色仍只被授予 SELECT，绝无 INSERT/UPDATE/DELETE/TRUNCATE/DDL。
# 这与运行角色 app_runtime 的 NOBYPASSRLS（租户隔离）明确不同——备份是全租户快照，必须能读全量。
BACKUP_ROLE = "backup_role"
BACKUP_ROLE_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
        CREATE ROLE {role} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS;
    END IF;
END
$$;
""".format(role=BACKUP_ROLE)


def apply_backup_role(engine: "Engine", password: str | None = None, dbname: str | None = None) -> None:
    """创建（若不存在）最小权限备份角色 backup_role，设置口令并授以**只读**最小权限。

    安全目标：备份脚本绝不以 owner/超级用户连接。backup_role 只具备：
      * LOGIN（可认证连接；NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE **BYPASSRLS**）；
      * CONNECT ON 当前数据库；
      * USAGE ON SCHEMA public；
      * SELECT ON 全部表/序列（含未来默认权限），用于 pg_dump 完整只读导出。

    **明确不授予** INSERT/UPDATE/DELETE/TRUNCATE/DDL；并显式 `REVOKE CREATE ON SCHEMA public`/`ON DATABASE`
    以兜底（PG14 及更早版本的 `public` schema 默认给 PUBLIC 授予 CREATE，不回收则备份角色可建表，违反最小权限）。
    因此任何写入/DDL 都会因 permission denied 而失败（下钻可验证：backup_role 无 DML/DDL 授权）。

    BYPASSRLS 仅用于让备份角色读出**全量**数据（含 RLS 表），不赋予任何写/DDL 权限；其授予需超级用户。

    需 CREATEROLE/超级用户权限（由 migrate 一次性服务以迁移角色执行）；不具备时抛异常由部署脚本处理。
    `password` 来自服务器环境密钥注入（BACKUP_ROLE_PASSWORD），为备份角色设置可认证口令；
    始终 ALTER 以幂等更新（每次迁移都同步最新注入口令）。传空则跳过口令设置（仅限本地开发）。
    """
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text(BACKUP_ROLE_SQL))
        if password:
            # PostgreSQL DDL (ALTER ROLE ... PASSWORD) 不接受绑定参数，需转义后以字面量执行。
            escaped = password.replace("'", "''")
            conn.execute(text(f"ALTER ROLE {BACKUP_ROLE} PASSWORD '{escaped}'"))
        if not dbname:
            dbname = conn.execute(text("SELECT current_database()")).scalar()
        conn.execute(text(f'GRANT CONNECT ON DATABASE "{dbname}" TO {BACKUP_ROLE}'))
        conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {BACKUP_ROLE}"))
        # 兜底：回收 public schema 的 CREATE（PG14 或默认放开 ACL 的环境），确保备份角色（及任何 PUBLIC 成员）
        # 都不能在 public 上做 DDL；运行角色 app_runtime 亦受益（仅 DML）。
        conn.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
        conn.execute(text(f"REVOKE CREATE ON SCHEMA public FROM {BACKUP_ROLE}"))
        conn.execute(text(f'REVOKE CREATE ON DATABASE "{dbname}" FROM {BACKUP_ROLE}'))
        conn.execute(text(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {BACKUP_ROLE}"))
        conn.execute(text(f"GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO {BACKUP_ROLE}"))
        # 未来由迁移角色新建的表/序列也自动授予只读（备份角色无需拥有写权限）。
        conn.execute(text(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {BACKUP_ROLE}"))
        conn.execute(text(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON SEQUENCES TO {BACKUP_ROLE}"))


def apply_business_schema(engine: "Engine") -> None:
    """建业务表 + 函数 + 业务表 RLS（在带 RLS 语义的连接上执行）。

    注意：`tenants` 为主数据，不启用 RLS；其余业务表全部启用。迁移角色应是 owner 或
    具备 CREATE/ALTER 权限的角色。运行时角色（建议 app_runtime）只做 GRANT 后的 DML。
    """
    from sqlalchemy import text

    with engine.begin() as conn:
        # 顺序：先建默认表（函数体引用这些表，LANGUAGE sql 在创建时校验表存在）→
        # 再建函数 → 最后启用 RLS policy（policy USING 引用函数）。
        for stmt in BUSINESS_SCHEMA_SQL:
            conn.execute(text(stmt))
        for stmt in SCOPE_FUNCTIONS_SQL:
            conn.execute(text(stmt))
        for stmt in RLS_TABLES_SQL:
            conn.execute(text(stmt))


def apply_checkpoint_rls(engine: "Engine") -> None:
    """对官方 checkpoint 表启用 RLS（前提：AsyncPostgresSaver.setup() 已建成这些表）。"""
    from sqlalchemy import text

    with engine.begin() as conn:
        for stmt in CHECKPOINT_RLS_SQL:
            conn.execute(text(stmt))


def apply_runtime_role(engine: "Engine", password: str | None = None) -> None:
    """创建（若不存在）运行角色 app_runtime，设置口令（可选）并授予业务表/序列/函数权限。

    运行角色必须是 `LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS`，
    这样 RLS 才能真正隔离（超级用户/表 owner 会绕过 RLS）。迁移/运维角色与运行角色分离；
    业务表由迁移角色（owner）创建，运行角色只获得 GRANT 后的 DML 权限，绝不成为表 owner。
    创建角色需 CREATEROLE/超级用户；不具备时抛异常，由部署脚本处理。

    `password` 来自服务器环境密钥注入（APP_RUNTIME_PASSWORD），为运行角色设置可认证口令；
    始终 ALTER 以幂等更新（每次迁移都同步最新注入口令）。传空则跳过口令设置（仅限本地开发）。
    """
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text(APP_RUNTIME_ROLE_SQL))
        if password:
            # PostgreSQL DDL (ALTER ROLE ... PASSWORD) 不接受绑定参数，需转义后以字面量执行。
            escaped = password.replace("'", "''")
            conn.execute(text(f"ALTER ROLE {APP_RUNTIME_ROLE} PASSWORD '{escaped}'"))
        conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {APP_RUNTIME_ROLE}"))
        conn.execute(text(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_RUNTIME_ROLE}"))
        conn.execute(text(
            f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_RUNTIME_ROLE}"))
        conn.execute(text(f"GRANT ALL ON FUNCTION app_current_tenant_id() TO {APP_RUNTIME_ROLE}"))
        conn.execute(text(
            f"GRANT ALL ON FUNCTION checkpoint_thread_in_current_tenant(TEXT) TO {APP_RUNTIME_ROLE}"))


def drop_everything(engine: "Engine") -> None:
    """清空本迁移创建的全部对象（用于可重复的恢复/清理测试）。"""
    from sqlalchemy import text

    tables = [
        "checkpoint_writes", "checkpoint_blobs", "checkpoints", "checkpoint_migrations",
        "stream_events", "streams", "executions", "approvals", "operations", "sessions",
        "memberships", "checkpoint_thread_scopes", "audit", "tenants",
        "shipping_events", "orders", "policy_documents",
    ]
    with engine.begin() as conn:
        for t in tables:
            conn.execute(text(f"DROP TABLE IF EXISTS {t} CASCADE;"))
        conn.execute(text("DROP FUNCTION IF EXISTS checkpoint_thread_in_current_tenant(TEXT) CASCADE;"))
        conn.execute(text("DROP FUNCTION IF EXISTS app_current_tenant_id() CASCADE;"))


# ---------------------------------------------------------------------------
# 官方 checkpoint 表（锁定 langgraph-checkpoint-postgres==3.1.2 的 MIGRATIONS 表结构）。
# 为避免运行时 saver.setup() 的 CREATE INDEX CONCURRENTLY 在事务内失败，这里在迁移阶段用
# 普通 CREATE INDEX 一次性建表；表结构逐列对齐官方 MIGRATIONS，索引改为非 CONCURRENTLY。
# ---------------------------------------------------------------------------
CHECKPOINT_SCHEMA_SQL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS checkpoint_migrations (
        v INTEGER PRIMARY KEY
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS checkpoints (
        thread_id TEXT NOT NULL,
        checkpoint_ns TEXT NOT NULL DEFAULT '',
        checkpoint_id TEXT NOT NULL,
        parent_checkpoint_id TEXT,
        type TEXT,
        checkpoint JSONB NOT NULL,
        metadata JSONB NOT NULL DEFAULT '{}',
        PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS checkpoint_blobs (
        thread_id TEXT NOT NULL,
        checkpoint_ns TEXT NOT NULL DEFAULT '',
        channel TEXT NOT NULL,
        version TEXT NOT NULL,
        type TEXT NOT NULL,
        blob BYTEA,
        PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS checkpoint_writes (
        thread_id TEXT NOT NULL,
        checkpoint_ns TEXT NOT NULL DEFAULT '',
        checkpoint_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        idx INTEGER NOT NULL,
        channel TEXT NOT NULL,
        type TEXT,
        blob BYTEA NOT NULL,
        PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
    )
    """,
    "ALTER TABLE checkpoint_blobs ALTER COLUMN blob DROP NOT NULL;",
    """
    ALTER TABLE checkpoint_writes ADD COLUMN IF NOT EXISTS task_path TEXT NOT NULL DEFAULT ''
    """,
    "CREATE INDEX IF NOT EXISTS checkpoints_thread_id_idx ON checkpoints(thread_id);",
    "CREATE INDEX IF NOT EXISTS checkpoint_blobs_thread_id_idx ON checkpoint_blobs(thread_id);",
    "CREATE INDEX IF NOT EXISTS checkpoint_writes_thread_id_idx ON checkpoint_writes(thread_id);",
]


def initialize_checkpoint_schema(engine: "Engine") -> None:
    """在迁移阶段一次性建立官方 checkpoint 表（非 CONCURRENTLY 索引）。"""
    from sqlalchemy import text

    with engine.begin() as conn:
        for stmt in CHECKPOINT_SCHEMA_SQL:
            conn.execute(text(stmt))


def initialize_all(engine: "Engine") -> None:
    """完整初始化：业务 schema + 官方 checkpoint + 全部 RLS。用于新建环境（幂等）。

    调用方应确保：连接角色具备 CREATE/ALTER 权限；运行时角色（建议 app_runtime）随后做 GRANT。
    """
    apply_business_schema(engine)
    initialize_checkpoint_schema(engine)
    apply_checkpoint_rls(engine)
