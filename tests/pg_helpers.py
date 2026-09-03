"""PostgreSQL 集成测试辅助（探测 DATABASE_URL 可用性）。"""
from __future__ import annotations

import os

from sqlalchemy import create_engine, text

from src.infrastructure.postgres_store import _to_sqlalchemy_url


def pg_dsn() -> str:
    return os.environ.get("DATABASE_URL", "")


def make_pg_engine():
    """尝试连接 PostgreSQL；可用则返回 SQLAlchemy engine，否则返回 None。"""
    dsn = pg_dsn()
    if not dsn:
        return None
    eng = create_engine(_to_sqlalchemy_url(dsn), pool_pre_ping=True)
    try:
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        return eng
    except Exception:
        eng.dispose()
        return None


def pg_available() -> bool:
    return make_pg_engine() is not None


def reset_pg_schema(engine) -> None:
    """清空并重建本迁移创建的全部对象（可重复初始化）。"""
    from src.infrastructure import migrations

    migrations.drop_everything(engine)
    migrations.initialize_all(engine)


APP_RUNTIME_USER = "app_runtime"
APP_RUNTIME_PASSWORD = "apppass"


def app_runtime_dsn() -> str:
    """在 DATABASE_URL 的 host/port/db 上换成 app_runtime 运行角色的连接串。"""
    from urllib.parse import urlparse, urlunparse

    dsn = pg_dsn()
    if not dsn:
        return ""
    p = urlparse(dsn)
    host = p.hostname or "127.0.0.1"
    port = f":{p.port}" if p.port else ""
    netloc = f"{APP_RUNTIME_USER}:{APP_RUNTIME_PASSWORD}@{host}{port}"
    return urlunparse((p.scheme, netloc, p.path, p.params, p.query, p.fragment))


def setup_runtime_role(engine) -> None:
    """用超级用户创建运行角色 app_runtime（NOBYPASSRLS）并授予业务表权限。

    这是 RLS 验证的前提：超级用户/表 owner 总是绕过 RLS；运行角色非 owner 且
    NOBYPASSRLS，RLS 才能真正隔离跨租户数据。
    """
    from sqlalchemy import text

    with engine.begin() as conn:
        exists = conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname=:r"),
                              {"r": APP_RUNTIME_USER}).scalar()
        if exists != 1:
            conn.execute(text(
                f"CREATE ROLE {APP_RUNTIME_USER} LOGIN PASSWORD '{APP_RUNTIME_PASSWORD}' "
                "NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
            ))
        conn.execute(text("GRANT USAGE ON SCHEMA public TO app_runtime"))
        conn.execute(text(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_runtime"))
        conn.execute(text(
            "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_runtime"))
        conn.execute(text(
            "GRANT ALL ON FUNCTION app_current_tenant_id() TO app_runtime"))
        conn.execute(text(
            "GRANT ALL ON FUNCTION checkpoint_thread_in_current_tenant(TEXT) TO app_runtime"))
