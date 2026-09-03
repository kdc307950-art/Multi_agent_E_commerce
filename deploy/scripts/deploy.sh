#!/usr/bin/env bash
# deploy.sh —— 一次性部署 preview 拓扑（幂等、安全、fail-closed）。
# 流程：检查机密 → 生成 TLS 证书 → 构建镜像 → 启动服务 → 等待健康 → 记录版本。
# 用法：bash deploy/scripts/deploy.sh [git-ref]
#   git-ref 缺省为当前 HEAD；用于发布回滚时以固定 commit 重建。
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

maybe_docker

REF="${1:-$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)}"

log "1/5 校验服务器机密注入"
bash "$SCRIPT_DIR/check_secrets.sh"

log "2/5 生成 TLS 证书（如缺失）"
bash "$SCRIPT_DIR/gen_certs.sh"

log "3/5 构建镜像（当前引用：$REF）"
compose build --pull

log "4/5 启动服务（前置依赖自动等待）"
compose up -d

log "5/5 等待 nginx 就绪（其依赖 frontend/api 健康后启动）"
for _ in $(seq 1 30); do
  if compose exec -T nginx wget -qO- -T 3 http://localhost/healthz >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

compose ps
mkdir -p "$DEPLOY_DIR/backups"
echo "deploy ref=$REF ts=$(date '+%FT%T')" >> "$DEPLOY_DIR/backups/rollback.log"
log "部署完成。请执行 deploy/scripts/healthcheck.sh 验收。"
warn "本 Git 引用：$REF（回滚用 deploy/scripts/rollback.sh $REF <backup-file>）"
