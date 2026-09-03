#!/usr/bin/env bash
# check_secrets.sh —— 校验 preview 必需的服务器机密已注入，缺失即失败（fail-closed）。
# 用法：bash deploy/scripts/check_secrets.sh
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

: "${ENV_FILE}"
[ -f "$ENV_FILE" ] || die "找不到密钥文件 $ENV_FILE。请先：cp deploy/.env.preview.example deploy/.env.preview 并填入真实机密。"

# 从 ENV_FILE 读取（兼容 KEY=VALUE 与注释）。
get() { grep -E "^$1=" "$ENV_FILE" | head -n1 | cut -d= -f2- | tr -d '"' | tr -d "'" || true; }

# 必需且不得为占位符的机密。
REQUIRED=(POSTGRES_PASSWORD REDIS_PASSWORD APP_RUNTIME_PASSWORD AUTH_JWT_SECRET LLM_API_KEY)
PLACEHOLDER_OPEN='<'
PLACEHOLDER_CLOSE='>'

missing=0
for k in "${REQUIRED[@]}"; do
  v="$(get "$k")"
  if [ -z "$v" ]; then
    warn "缺少必需密钥 $k（为空）。"
    missing=1
  elif [[ "$v" == *"$PLACEHOLDER_OPEN"* || "$v" == *"$PLACEHOLDER_CLOSE"* ]]; then
    warn "密钥 $k 仍是占位符（$v），须替换为真实注入值。"
    missing=1
  fi
done

# AUTH_JWT_SECRET 为空时 preview 的 api 启动即 fail-closed（见 src/main.py）。
if [ "$(get AUTH_JWT_SECRET || true)" = "<inject>" ] || [ -z "$(get AUTH_JWT_SECRET || true)" ]; then
  warn "AUTH_JWT_SECRET 缺失/占位 → 受限环境 api 启动将 fail-closed（有意为之）。"
fi

if [ "$missing" -ne 0 ]; then
  die "存在缺失或占位的机密，拒绝部署（fail-closed）。"
fi

log "机密检查通过：$(IFS=,; echo "${REQUIRED[*]}") 均已注入。"
