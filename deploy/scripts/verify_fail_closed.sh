#!/usr/bin/env bash
# verify_fail_closed.sh —— 在 preview 启动语义上验证 fail-closed（硬性验收）。
# 在一次性 api 容器内执行：受限环境 + Mock 认证、或 + real 但缺 AUTH_JWT_SECRET，均应启动即失败。
# 任一项未满足即非零退出（fail-closed 闸门）。
# 用法：bash deploy/scripts/verify_fail_closed.sh
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

maybe_docker

log "在一次性 api 容器内验证受限环境 fail-closed（需已构建 api 镜像）"
compose run --rm --no-deps api python - <<'PY'
import sys
from src.config import Settings
from src.main import create_app

def expect_raise(label, settings, exc=RuntimeError):
    try:
        create_app(settings=settings)
    except exc:
        print(f"  [pass] {label}")
        return True
    print(f"  [FAIL] {label}: 预期抛出 {exc.__name__}")
    return False

def expect_ok(label, settings):
    try:
        create_app(settings=settings)
    except Exception as e:
        print(f"  [FAIL] {label}: 不应抛出，实际 {type(e).__name__}: {e}")
        return False
    print(f"  [pass] {label}")
    return True

ok = True
ok &= expect_raise("preview + mock 启动 fail-closed", Settings(env="preview", auth_backend="mock"))
ok &= expect_raise("production + mock 启动 fail-closed", Settings(env="production", auth_backend="mock"))
ok &= expect_raise("preview + real + 缺 AUTH_JWT_SECRET 启动 fail-closed",
                   Settings(env="preview", auth_backend="real", auth_jwt_secret=""))
ok &= expect_ok("preview + real + 提供 AUTH_JWT_SECRET 允许构建",
                Settings(env="preview", auth_backend="real", auth_jwt_secret="s3cret"))
if not ok:
    print("FAIL_CLOSED_VERIFY: FAIL")
    sys.exit(1)
print("FAIL_CLOSED_VERIFY: OK")
PY
