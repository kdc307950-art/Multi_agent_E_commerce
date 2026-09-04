#!/usr/bin/env bash
# deploy.sh —— 一次性部署 preview 拓扑（幂等、安全、fail-closed）。
# 部署基线（t4）：严格拒绝脏工作区 → 从指定 git 引用检出【干净 worktree】构建 → 发布记录回填。
#
# 部署闸门（依次通过后才允许构建/启动）：
#   0. 工作区干净度（fail-closed；除非显式 --allow-dirty）
#   1. 部署前检查（pre_deploy_checks.sh：仓库干净 / ref 可解析 / compose config --quiet / check_secrets）
#   2. 严格上线门控 verify_launch_gate.py --strict
#   3. Compose 配置校验 compose config --quiet
#   4. 生成证书 → 【干净 worktree】构建（build context 仅含该引用下已提交源码，可复现镜像）
#   5. fail-closed 验证 verify_fail_closed.sh
#   6. 启动服务 → 等待 nginx → 回写发布记录与 rollback.log（commit/digest/构建时间/配置版本）
#
# 用法：bash deploy/scripts/deploy.sh [--allow-dirty] [git-ref]
#   git-ref 缺省=当前 HEAD；必须是已提交的 tag/commit/分支（干净 tree）。
#   --allow-dirty 显式放行脏工作区（仅供应急/调试；不推荐，发布应走干净 ref 的干净 worktree 构建）。
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

maybe_docker

# ---- 参数解析：--allow-dirty 标志 + 可选 git-ref ----
ALLOW_DIRTY=0
REF=""
while [ $# -gt 0 ]; do
  case "$1" in
    --allow-dirty) ALLOW_DIRTY=1; shift ;;
    *) REF="$1"; shift ;;
  esac
done
if [ -z "$REF" ]; then
  REF="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
fi

# 复用 common.sh 的 env_get 从 ENV_FILE 读取键值当作 shell 变量导出。
export_gate_var() {
  local k="$1" v
  v="$(env_get "$k")"
  if [ -n "$v" ]; then export "$k=$v"; fi
}

# 选取可用于运行 verify_launch_gate.py 的 Python（优先仓库 venv；否则 python3/python）。
find_python() {
  local cand
  for cand in "$REPO_ROOT/.venv/bin/python" "$REPO_ROOT/.venv/Scripts/python.exe" python3 python; do
    if [ -x "$cand" ] || command -v "$cand" >/dev/null 2>&1; then PYTHON_BIN="$cand"; return 0; fi
  done
  return 1
}

PYTHON_BIN="${PYTHON_BIN:-}"
find_python || die "未找到 Python，无法执行严格上线门控（verify_launch_gate.py --strict）。请安装 Python 或设置 PYTHON_BIN。"

log "0/8 工作区干净度检查（fail-closed）"
if repo_is_dirty; then
  if [ "$ALLOW_DIRTY" -eq 1 ]; then
    warn "检测到未提交/未跟踪变更，但已显式 --allow-dirty：将使用指定 ref（$REF）的干净 worktree 构建，主工作树变更不进入 build context。"
  else
    die "工作区不干净（git status --porcelain 非空），拒绝构建（fail-closed）。请先 'git commit'/'git stash' 达到干净状态，或用 '--allow-dirty' 显式放行（不推荐）。"
  fi
else
  log "工作区干净：git status --porcelain 为空。"
fi

log "1/8 部署前检查（pre_deploy_checks.sh）"
# 把 --allow-dirty 透传给 pre_deploy_checks，使其在显式放行时跳过干净度闸门。
PRECHK_ARGS=( "$REF" )
if [ "$ALLOW_DIRTY" -eq 1 ]; then PRECHK_ARGS+=( --allow-dirty ); fi
bash "$SCRIPT_DIR/pre_deploy_checks.sh" "${PRECHK_ARGS[@]}"

log "2/8 校验服务器机密注入 + 发布配置版本"
bash "$SCRIPT_DIR/check_secrets.sh"

log "3/8 严格上线门控核验（verify_launch_gate.py --strict）"
export_gate_var ENV
export_gate_var STORAGE_BACKEND
export_gate_var LAUNCH_ALLOWED_TENANTS
export_gate_var EXECUTION_MODE
export_gate_var EXECUTION_PROVIDER
export_gate_var LAUNCH_REQUIRE_APPROVAL
export_gate_var LAUNCH_FULL_AUDIT
export_gate_var LAUNCH_MANUAL_REVIEW
"$PYTHON_BIN" "$REPO_ROOT/scripts/verify_launch_gate.py" --strict

log "4/8 Compose 配置校验（真实仓库 compose）"
compose config --quiet

log "5/8 生成 TLS 证书（如缺失）"
bash "$SCRIPT_DIR/gen_certs.sh"

log "6/8 从干净 worktree 构建镜像（引用：$REF）"
# 环境配置版本（发布记录必填）：优先取自 ENV_FILE；未设则回落到 src/config.py 的 deploy_config_version 默认。
BUILD_CONFIG_VERSION="$(env_get DEPLOY_CONFIG_VERSION || true)"
if [ -z "$BUILD_CONFIG_VERSION" ]; then
  BUILD_CONFIG_VERSION="$(config_version_default)"
  warn "ENV_FILE 未设置 DEPLOY_CONFIG_VERSION，回落到 src/config.py 默认：${BUILD_CONFIG_VERSION:-<未定义>}（发布记录已明示）。"
else
  log "DEPLOY_CONFIG_VERSION=$BUILD_CONFIG_VERSION（取自 ENV_FILE）"
fi
BUILD_TIMESTAMP="$(date '+%Y-%m-%d %H:%M:%S %z')"
BUILD_REPRODUCIBLE="待实测"
build_from_worktree "$REF"

log "7/8 受限环境 fail-closed 验证（需已构建 api 镜像）"
bash "$SCRIPT_DIR/verify_fail_closed.sh"

log "8/8 启动服务（前置依赖自动等待）"
compose up -d

log "等待 nginx 就绪（其依赖 frontend/api 健康后启动）"
for _ in $(seq 1 30); do
  if compose exec -T nginx wget -qO- -T 3 http://localhost/healthz >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

compose ps
mkdir -p "$DEPLOY_DIR/backups"
echo "deploy ref=$REF commit=$(git_ref_commit "$REF") config_version=${BUILD_CONFIG_VERSION} build_ts=${BUILD_TIMESTAMP} digests=$(IFS=,; echo "${BUILD_DIGESTS[*]}")" >> "$DEPLOY_DIR/backups/rollback.log"

log "回写发布记录（commit/digest/构建时间/配置版本）"
RECORD_DIR="$DEPLOY_DIR/records"
mkdir -p "$RECORD_DIR"
RECORD_FILE="${RECORD_DIR}/PRODUCTION_DEPLOYMENT_record-$(date +%Y%m%d-%H%M%S).md"
write_publish_record "$RECORD_FILE" "$REF"

log "部署完成。发布记录：$RECORD_FILE"
warn "请执行 deploy/scripts/healthcheck.sh 验收。"
warn "本 Git 引用：$REF（commit=$(git_ref_commit "$REF")）；回滚用 deploy/scripts/rollback.sh <backup-file> $REF"
