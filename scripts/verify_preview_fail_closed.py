"""verify_preview_fail_closed.py — 校验 preview 受限环境的启动期 fail-closed。

覆盖《preview 部署清单》验收项：
- 受限环境（preview/production）+ AUTH_BACKEND=mock → create_app 启动即抛 RuntimeError；
- 受限环境 + AUTH_BACKEND=real 但 AUTH_JWT_SECRET 为空 → create_app 启动即抛 RuntimeError；
- 受限环境 + real + 提供 AUTH_JWT_SECRET → 允许构建（不抛）。

任何一项未满足则非零退出，便于本地（venv）与一次性容器/Docker 中作为闸门。

用法：
    python scripts/verify_preview_fail_closed.py
"""
from __future__ import annotations

import sys

from src.config import Settings
from src.main import create_app


def _expect_raise(label: str, settings: Settings, exc: type[Exception]) -> bool:
    try:
        create_app(settings=settings)
    except exc:
        print(f"  [pass] {label}")
        return True
    print(f"  [FAIL] {label}: 预期抛出 {exc.__name__}，但未抛出")
    return False


def _expect_ok(label: str, settings: Settings) -> bool:
    try:
        create_app(settings=settings)
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] {label}: 不应抛出，实际 {type(exc).__name__}: {exc}")
        return False
    print(f"  [pass] {label}")
    return True


def main() -> int:
    ok = True
    ok &= _expect_raise("preview + mock → RuntimeError",
                        Settings(env="preview", auth_backend="mock"), RuntimeError)
    ok &= _expect_raise("production + mock → RuntimeError",
                        Settings(env="production", auth_backend="mock"), RuntimeError)
    ok &= _expect_raise("preview + real + 缺 AUTH_JWT_SECRET → RuntimeError",
                        Settings(env="preview", auth_backend="real", auth_jwt_secret=""),
                        RuntimeError)
    ok &= _expect_raise("production + real + 缺 AUTH_JWT_SECRET → RuntimeError",
                        Settings(env="production", auth_backend="real", auth_jwt_secret=""),
                        RuntimeError)
    ok &= _expect_ok("preview + real + 提供 AUTH_JWT_SECRET → 允许构建",
                     Settings(env="preview", auth_backend="real", auth_jwt_secret="s3cret-verify"))
    ok &= _expect_ok("development + mock → 允许构建（本地开发）",
                     Settings(env="development", auth_backend="mock"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
