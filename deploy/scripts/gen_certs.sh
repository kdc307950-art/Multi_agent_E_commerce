#!/usr/bin/env bash
# gen_certs.sh —— 为 preview 反向代理生成自签名 TLS 证书（仅用于内部预发布验证）。
# 生产替换为受信 CA 或编排层注入的证书链。生成物在 deploy/secrets/certs/（已 gitignore）。
# 用法：bash deploy/scripts/gen_certs.sh [domain]
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

DOMAIN="${1:-preview.local}"
CERT_DIR="$DEPLOY_DIR/secrets/certs"
mkdir -p "$CERT_DIR"

CRT="$CERT_DIR/server.crt"
KEY="$CERT_DIR/server.key"

if [ -f "$CRT" ] && [ -f "$KEY" ]; then
  log "证书已存在：$CRT / $KEY（跳过生成；如需更换请先删除）。"
  exit 0
fi

log "为域 $DOMAIN 生成自签名证书（有效期 365 天）..."
openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
  -keyout "$KEY" -out "$CRT" \
  -subj "/CN=$DOMAIN" \
  -addext "subjectAltName=DNS:$DOMAIN,DNS:localhost,IP:127.0.0.1" >/dev/null 2>&1

chmod 600 "$KEY"
log "证书已生成：$CRT / $KEY"
log "提示：自签名证书浏览器会告警；生产/预发布受信验收请替换为受信 CA 证书并把常用名/主题备用名指向你的域名。"
