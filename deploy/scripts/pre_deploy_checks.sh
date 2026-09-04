#!/usr/bin/env bash
# pre_deploy_checks.sh —— 部署前检查（deploy.sh 第 1/8 步调用；也可独立运行）。
# 在构建前依次核查：
#   ① 仓库干净度（git status --porcelain 为空；fail-closed，除非显式 --allow-dirty）
#   ② 指定 git 引用可解析为 commit（tag/commit/branch/HEAD）
#   ③ 该引用下存在可构建的 Dockerfile / docker-compose.preview.yml（build context 来自受控文件）
#   ④ compose config --quiet（真实仓库 compose 文件：${VAR:?} 必需变量与拓扑合法性）
#   ⑤ check_secrets（必需机密 + DEPLOY_CONFIG_VERSION 非空/非占位 + 不允许明文/弱哈希凭据）
# 任何一项失败即非零退出（fail-closed），拒绝部署。
# 用法：bash deploy/scripts/pre_deploy_checks.sh [git-ref] [--allow-dirty]
#   git-ref 缺省 HEAD；--allow-dirty 跳过①的干净度门禁（不推荐）。
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

maybe_docker

ALLOW_DIRTY=0
REF=""
while [ $# -gt 0 ]; do
  case "$1" in
    --allow-dirty) ALLOW_DIRTY=1; shift ;;
    *) REF="$1"; shift ;;
  esac
done
if [ -z "$REF" ]; then REF="HEAD"; fi

fail=0

log "部署前检查：ref=$REF allow_dirty=$ALLOW_DIRTY"

# ① 仓库干净度
if repo_is_dirty; then
  if [ "$ALLOW_DIRTY" -eq 1 ]; then
    warn "① 工作区脏但已显式 --allow-dirty：跳过干净度门禁（不推荐，build context 仍来自 ref 干净 worktree）。"
  else
    warn "① 工作区不干净（git status --porcelain 非空），拒绝部署（fail-closed）。请先 commit/stash。"
    fail=1
  fi
else
  log "① 仓库干净：git status --porcelain 为空。"
fi

# ② 引用可解析
FULL="$(git_ref_commit "$REF")"
if [ -z "$FULL" ]; then
  warn "② git 引用不可解析：$REF（须为已提交的 commit/tag/分支/HEAD）。"
  fail=1
else
  log "② git 引用 $REF → commit ${FULL:0:12}…（${FULL}）。"
fi

# ③ 该引用下存在可构建文件（受控 build context）
if [ -n "$FULL" ]; then
  for f in Dockerfile docker-compose.preview.yml; do
    if git -C "$REPO_ROOT" cat-file -e "$FULL:$f" 2>/dev/null; then
      log "    引用 ${REF} 存在 ${f}（作为 build context 来源）。"
    else
      warn "    引用 ${REF} 下缺失 ${f}（无法构建）。"
      fail=1
    fi
  done
fi

# ④ compose config --quiet（真实仓库 compose）
if compose config --quiet; then
  log "③ compose config 合法（必需变量与拓扑校验通过）。"
else
  warn "③ compose config 不合法（缺必需变量或拓扑错误）。"
  fail=1
fi

# ⑤ check_secrets（含 DEPLOY_CONFIG_VERSION/弱哈希/明文校验）
if bash "$SCRIPT_DIR/check_secrets.sh"; then
  log "④ check_secrets 通过。"
else
  warn "④ check_secrets 失败（fail-closed）。"
  fail=1
fi

if [ "$fail" -ne 0 ]; then
  die "部署前检查未通过（fail-closed），拒绝部署。"
fi
log "部署前检查全部通过（仓库干净 / ref 可解析 / 受控 build context / compose 合法 / 密钥与配置版本）。"
