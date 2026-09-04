"""数据库迁移 CLI 入口（`python -m src.infrastructure.migrate_cli`）。

在 Docker Compose 中作为一次性初始化服务运行，创建业务表、官方 checkpoint 表、
RLS 与函数，并创建/更新建议的运行角色 app_runtime（迁移/运行角色分离）。

连接角色约定：本入口使用**迁移角色**（`database_migrator_url`，缺省回退 `database_url`，
但该回退仅限本地开发），需 CREATE/ALTER 权限；绝不会把表 owner 的 DSN 交给 api/worker。
"""
from __future__ import annotations

from sqlalchemy import create_engine

from src.config import get_settings
from src.infrastructure import migrations
from src.infrastructure.postgres_store import _to_sqlalchemy_url


def main() -> None:
    settings = get_settings()
    # 迁移角色优先；留空则回退运行 DSN（仅限本地开发，连接角色即 owner）。
    migrate_url = settings.database_migrator_url or settings.database_url
    if not migrate_url:
        raise SystemExit("缺少 DATABASE_URL / DATABASE_MIGRATOR_URL，无法迁移。")
    if migrate_url == settings.database_url and settings.is_restricted_env:
        raise SystemExit(
            "受限环境（preview/production）必须配置 DATABASE_MIGRATOR_URL 以分离迁移角色与"
            "运行角色；使用同一连接运行迁移属于危险配置，拒绝执行。"
        )
    engine = create_engine(_to_sqlalchemy_url(migrate_url), pool_pre_ping=True)
    migrations.initialize_all(engine)
    # 迁移/运行角色分离：创建/更新运行角色 + 最小权限备份角色（需 CREATEROLE/超级用户）。
    try:
        migrations.apply_runtime_role(engine, password=settings.app_runtime_password)
        migrations.apply_backup_role(engine, password=settings.backup_role_password)
        print("migrations applied; runtime role and backup role ensured.")
    except Exception as exc:  # noqa: BLE001
        if settings.is_restricted_env:
            # 受限环境运行/备份角色缺失会导致 api/worker 或备份失败，属必败配置 → fail-closed。
            raise SystemExit(f"受限环境未能将运行角色/备份角色就绪：{exc}") from exc
        print(f"migrations applied; runtime/backup role skipped: {exc}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
