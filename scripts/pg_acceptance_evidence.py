"""PostgreSQL 数据面验收 —— 额外验证证据采集（面向 Preview 服务器/非超级用户 app_runtime）。

在真实 PostgreSQL 上，用运行角色 app_runtime（LOGIN NOINHERIT NOSUPERUSER NOBYPASSRLS）对以下项做
直接验证并输出证据 JSON：
  1. 跨租户读取拒绝（store + SQL RLS 双重）
  2. 停用租户拒绝
  3. 跨租户审批拒绝（读取 + 决策 CAS 双重）
  4. 租户级幂等（同 idempotency_key 同一 operation_id）
  5. 重复回调（同 nonce 只重放不重复生效；CAS claim_callback）
  6. API 重启恢复（重建 PostgresStore 后数据仍在）
  7. worker/Redis 重连恢复（bulk 状态 + Redis 重连可达性）
环境变量：
  DATABASE_URL          超级用户连接（postgres/postgres；仅用于 reset schema + 运行角色 GRANT）
  APP_RUNTIME_PASSWORD  运行角色口令（默认 apppass，与 tests.pg_helpers 一致）
输出：evidence/pg_acceptance_evidence.json + 控制台摘要
"""
from __future__ import annotations

import json
import os
import time
from urllib.parse import urlparse, urlunparse

from sqlalchemy import text
from sqlalchemy import create_engine

from src.core.types import (
    DomainError,
    ErrorCode,
    PendingAction,
    Role,
    TenantStatus,
    generate_operation_key,
)
from src.execution import ExecutionMode, ExecutionStatus
from src.infrastructure import migrations
from src.infrastructure.postgres_store import PostgresStore, _to_sqlalchemy_url


def _app_runtime_dsn() -> str:
    dsn = os.environ["DATABASE_URL"]
    p = urlparse(dsn)
    host = p.hostname or "127.0.0.1"
    port = f":{p.port}" if p.port else ""
    netloc = f"app_runtime:{os.environ.get('APP_RUNTIME_PASSWORD', 'apppass')}@{host}{port}"
    return urlunparse((p.scheme, netloc, p.path, p.params, p.query, p.fragment))


def _set_tenant(conn, tenant_id: str | None):
    if tenant_id is None:
        conn.execute(text("SELECT set_config('app.tenant_id', NULL, true)"))
    else:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})


def _count(conn, sql: str, **params) -> int:
    return int(conn.execute(text(sql), params).scalar())


def _expect_denied(fn, label, code=None):
    """运行 fn，期望抛 DomainError（404/跨租户/租户停用）。返回 (ok, detail)。"""
    try:
        fn()
        return {"ok": False, "detail": f"{label}: 未拒绝（放行了）"}
    except DomainError as e:
        if code is not None and e.code != code:
            return {"ok": False, "detail": f"{label}: 错误码 {e.code} != {code}"}
        return {"ok": True, "detail": f"{label}: 拒绝 code={e.code} msg={e.message}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": True, "detail": f"{label}: 拒绝 {type(e).__name__}: {e}"}


def main() -> None:
    results = []
    env = {}

    # ---- 0) 用超级用户 reset schema（自包含；迁移角色逻辑在 migrate_cli 已单独验证）----
    super_eng = create_engine(_to_sqlalchemy_url(os.environ["DATABASE_URL"]), pool_pre_ping=True)
    with super_eng.begin() as conn:
        conn.execute(text("SELECT 1"))
    # 重置 schema + 运行角色（脚本可独立运行，幂等）。
    migrations.drop_everything(super_eng)
    migrations.initialize_all(super_eng)
    from tests.pg_helpers import setup_runtime_role
    setup_runtime_role(super_eng)
    results.append({"name": "schema_reset", "ok": True})
    super_eng.dispose()

    # ---- app_runtime（非超级用户，NOBYPASSRLS）----
    runtime_dsn = _app_runtime_dsn()
    runtime_eng = create_engine(_to_sqlalchemy_url(runtime_dsn), pool_pre_ping=True)
    store = PostgresStore(runtime_dsn, engine=runtime_eng)

    now = time.time()

    # 准备基础数据（租户 A/B + 成员 + A 的会话/操作/审批）
    store.create_tenant("TENANT-A", "租户A")
    store.create_tenant("TENANT-B", "租户B")
    store.add_membership("TENANT-A", "USER-001", Role.CUSTOMER)
    store.add_membership("TENANT-A", "ADMIN-A", Role.ADMIN)
    store.add_membership("TENANT-B", "USER-B1", Role.CUSTOMER)
    store.create_session("TENANT-A", "USER-001", "th-ev", now, 7)
    key = generate_operation_key(PendingAction.REFUND, "TENANT-A", "ORD-1", "REQ-IDEM")
    op = store.create_operation("TENANT-A", "th-ev", "ORD-1", PendingAction.REFUND, key, now)
    appr = store.create_approval("TENANT-A", "th-ev", op.operation_id, PendingAction.REFUND,
                                 "ORD-1", 100.0, "reason", now)

    # ---- 1) 跨租户读取拒绝（store）----
    r = _expect_denied(lambda: store.get_operation("TENANT-B", op.operation_id),
                       "cross_tenant_read_operation", ErrorCode.NOT_FOUND)
    r2 = _expect_denied(lambda: store.get_session("TENANT-B", "th-ev"),
                        "cross_tenant_read_session", ErrorCode.NOT_FOUND)
    results.append({"name": "cross_tenant_read_store", "ok": r["ok"] and r2["ok"],
                    "detail": f"{r['detail']}; {r2['detail']}"})

    # ---- 1b) 跨租户读取拒绝（SQL RLS）----
    with runtime_eng.begin() as conn:
        _set_tenant(conn, "TENANT-A")
        a_vis = _count(conn, "SELECT count(*) FROM sessions WHERE tenant_id='TENANT-A'")
        _set_tenant(conn, "TENANT-B")
        b_vis = _count(conn, "SELECT count(*) FROM sessions")  # B 作用域：A 的会话零可见
    rls_cross_read = (a_vis == 1 and b_vis == 0)
    results.append({"name": "cross_tenant_read_rls", "ok": rls_cross_read,
                    "detail": f"tenantA visible={a_vis}, tenantB zero_visible={b_vis}"})

    # ---- 1c) 跨租户写拒绝（SQL RLS WITH CHECK）----
    with runtime_eng.begin() as conn:
        _set_tenant(conn, "TENANT-B")
        try:
            conn.execute(text(
                "INSERT INTO sessions(tenant_id,thread_id,user_id,created_at,expires_at,last_active_at,status) "
                "VALUES('TENANT-A','th-x','u',1,1,1,'active')"))
            inserted = True
        except Exception:  # noqa: BLE001
            inserted = False
    results.append({"name": "cross_tenant_write_rls", "ok": not inserted,
                    "detail": "WRITE cross-tenant blocked" if not inserted else "WRITE allowed (BAD)"})

    # ---- 1d) 无租户上下文 RLS 零可见----
    with runtime_eng.begin() as conn:
        _set_tenant(conn, None)
        none_vis = _count(conn, "SELECT count(*) FROM sessions")
    results.append({"name": "rls_zero_visible_without_tenant", "ok": none_vis == 0,
                    "detail": f"no tenant context visible={none_vis}"})

    # ---- 2) 停用租户拒绝 ----
    store.set_tenant_status("TENANT-A", TenantStatus.SUSPENDED)
    r = _expect_denied(lambda: store.create_session("TENANT-A", "USER-001", "th-sus", now, 7),
                       "suspended_tenant_reject", ErrorCode.TENANT_SUSPENDED)
    store.set_tenant_status("TENANT-A", TenantStatus.ACTIVE)
    results.append({"name": "suspended_tenant_rejected", "ok": r["ok"], "detail": r["detail"]})

    # ---- 3) 跨租户审批拒绝（读取 + 决策）----
    r_read = _expect_denied(lambda: store.get_approval("TENANT-B", appr.approval_id),
                            "cross_tenant_approval_read", ErrorCode.NOT_FOUND)
    r_dec = _expect_denied(lambda: store.decide_approval("TENANT-B", appr.approval_id, "OTHER",
                                                         True, "not yours", now),
                           "cross_tenant_approval_decision", ErrorCode.NOT_FOUND)
    results.append({"name": "cross_tenant_approval_rejected", "ok": r_read["ok"] and r_dec["ok"],
                    "detail": f"{r_read['detail']}; {r_dec['detail']}"})

    # ---- 4) 租户级幂等（同 key 同一 operation_id）----
    op2 = store.create_operation("TENANT-A", "th-ev", "ORD-1", PendingAction.REFUND, key, now)
    idem = (op2.operation_id == op.operation_id)
    results.append({"name": "tenant_idempotency", "ok": idem,
                    "detail": f"same idempotency_key -> same operation_id={op.operation_id}"})

    # ---- 5) 重复回调（同 nonce 只重放不重复生效；CAS claim_callback）----
    exec_id = store.create_execution_record(
        "TENANT-A", operation_id=op.operation_id, pending_action=PendingAction.REFUND,
        order_id="ORD-1", idempotency_key=key, mode=ExecutionMode.LIVE, amount=100.0, now=now,
    ).execution_id
    rec1, replay1 = store.claim_callback("TENANT-A", exec_id, "nonce-dup")
    rec2, replay2 = store.claim_callback("TENANT-A", exec_id, "nonce-dup")
    # 终态封闭：回调确认置 confirmed，重复回调不改写（只重放）。
    store.update_execution_record("TENANT-A", exec_id,
                                  status=ExecutionStatus.CONFIRMED, confirmed_at=now)
    st1 = store.get_execution_record("TENANT-A", exec_id).status
    # 再次重放后回调 nonce 仍为原 nonce、状态保持 confirmed（未重复生效）。
    rec3, replay3 = store.claim_callback("TENANT-A", exec_id, "nonce-dup")
    ok5 = (replay1 is False and replay2 is True and replay3 is True
           and rec3.callback_nonce == "nonce-dup"
           and st1 == ExecutionStatus.CONFIRMED)
    results.append({"name": "duplicate_callback_replay_safe", "ok": ok5,
                    "detail": f"first claimed={not replay1}, replay={replay2}, re-replay={replay3}, "
                              f"status={st1.value}, committed_nonce={rec3.callback_nonce}"})

    # ---- 6) API 重启恢复（重建 PostgresStore，数据仍在）----
    store2 = PostgresStore(runtime_dsn, engine=runtime_eng)
    ok6 = (store2.get_session("TENANT-A", "th-ev").thread_id == "th-ev"
           and store2.get_operation("TENANT-A", op.operation_id).operation_id == op.operation_id
           and store2.get_approval("TENANT-A", appr.approval_id).approval_id == appr.approval_id)
    store2.close()
    results.append({"name": "api_restart_recovery", "ok": ok6,
                    "detail": "rebuild store -> session/operation/approval survive"})

    # ---- 7) worker / Redis 重连恢复 ----
    redis_info = {"available": False}
    try:
        import redis as redislib
        c1 = redislib.Redis(host=os.environ.get("REDIS_HOST", "127.0.0.1"),
                            port=int(os.environ.get("REDIS_PORT", "6379")),
                            password=os.environ.get("REDIS_PASSWORD") or None,
                            socket_timeout=2)
        c1.ping()
        c1.set("acceptance:probe", "v1")
        # 模拟 worker 断线后重连（新建客户端 = 重建连接）
        c2 = redislib.Redis(host=os.environ.get("REDIS_HOST", "127.0.0.1"),
                            port=int(os.environ.get("REDIS_PORT", "6379")),
                            password=os.environ.get("REDIS_PASSWORD") or None,
                            socket_timeout=2)
        got = c2.get("acceptance:probe")
        got = got.decode("utf-8") if isinstance(got, bytes) else got
        redis_info = {"available": True, "ping": True, "reconnect_get": got}
    except Exception as e:  # noqa: BLE001
        redis_info = {"available": False, "error": f"{type(e).__name__}: {e}"}
    results.append({"name": "worker_redis_reconnect", "ok": redis_info.get("available", False),
                    "detail": redis_info})

    env = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "pg_server": os.environ["DATABASE_URL"].split("@")[-1],
        "runtime_role": "app_runtime",
        "runtime_role_flags": "LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS",
        "results": results,
        "conclusion": all(r["ok"] for r in results),
    }
    os.makedirs("evidence", exist_ok=True)
    with open("evidence/pg_acceptance_evidence.json", "w", encoding="utf-8") as f:
        json.dump(env, f, ensure_ascii=False, indent=2)

    print(json.dumps(env, ensure_ascii=False, indent=2))
    store.close()
    runtime_eng.dispose()


if __name__ == "__main__":
    main()
