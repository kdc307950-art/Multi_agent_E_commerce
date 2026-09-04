#!/usr/bin/env bash
# verify_tls.sh —— 生产受信 TLS 接入验收
# 校验三项：
#   [A] nginx prod.conf 语义正确：80 端口仅 /healthz 200 + 其余 301→https；443 终结 TLS 并同源代理 /api。
#   [B] HTTPS 仅 443 终结：80(http) 不提供应用（仅 healthz/301）；443 可完成 TLS 握手。
#   [C] 证书链有效性 + 域名 SAN 匹配：leaf 有效期内、非自签、链可验（受信根）、SAN 含正式域名。
#
# 用法：
#   bash deploy/scripts/verify_tls.sh \
#       --host 127.0.0.1 --https-port 8843 --http-port 8080 \
#       --expect-domain admin.after-sales.example [--cacert /path/to/ca.pem | --require-trusted]
#
# 说明：
#   - 生产栈宿主端口为 8080(->nginx:80) / 8843(->nginx:443)，故默认 http-port=8080、https-port=8843。
#     （nginx 容器内端口为 80/443；宿主高端口为 preview 占用 80/443 所致。）
#   - 受信链验证需要受信根。传 --cacert（受信 CA 根/中间）或 --require-trusted 走系统信任库，
#     否则仅做「有效期内 + 非自签」并明确标注「链信任未验证」。
#   - 任一关键项失败即非零退出（可作为放量前闸门）。
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

HOST="127.0.0.1"
HTTPS_PORT=443
HTTP_PORT=80
EXPECT_DOMAIN=""
CACERT=""
REQUIRE_TRUSTED=0
OPENSSL="${OPENSSL:-openssl}"
NGINX_CONF="$REPO_ROOT/deploy/nginx/prod.conf"

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '\033[1;32m  [OK]\033[0m %s\n' "$*"; }
bad() { FAIL=$((FAIL+1)); printf '\033[1;31m  [FAIL]\033[0m %s\n' "$*"; }
warn(){ printf '\033[1;33m  [WARN]\033[0m %s\n' "$*"; }

usage() {
  cat >&2 <<EOF
verify_tls.sh [options]
  --host H            目标主机（默认 127.0.0.1）
  --https-port P      443 宿主端口（默认 443；生产 8843）
  --http-port P       80  宿主端口（默认 80；生产 8080）
  --expect-domain D   期望正式域名（用于 SAN/server_name 匹配，必填于生产）
  --cacert F          受信根/中间 CA（用于 openssl verify 链验证）
  --require-trusted   强制链信任验证（无 --cacert 时用系统信任库）
  --bin O             未使用（保留）
EOF
  exit 1
}

while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST="${2:-}"; shift 2 ;;
    --https-port) HTTPS_PORT="${2:-}"; shift 2 ;;
    --http-port) HTTP_PORT="${2:-}"; shift 2 ;;
    --expect-domain) EXPECT_DOMAIN="${2:-}"; shift 2 ;;
    --cacert) CACERT="${2:-}"; shift 2 ;;
    --require-trusted) REQUIRE_TRUSTED=1; shift 1 ;;
    *) echo "未知参数：$1" >&2; usage ;;
  esac
done

command -v curl >/dev/null 2>&1 || die "未找到 curl。"
[ -f "$NGINX_CONF" ] || die "未找到 nginx 配置：$NGINX_CONF"
[ -n "$EXPECT_DOMAIN" ] || die "生产验收必须提供 --expect-domain <正式域名>（用于 SAN/server_name 匹配）。"

# ===================== [A] nginx prod.conf 语义静态核对 =====================
echo "[A] nginx prod.conf 语义（应：80 仅 healthz/301；443 终结 TLS；同源代理 /api）"
grep -qE '^\s*listen\s+80\s*;' "$NGINX_CONF" && ok "80 server 存在" || bad "80 server 缺失（listen 80）"
grep -qE 'location\s*=\s*/healthz' "$NGINX_CONF" && ok "80 有 /healthz 精确匹配" || bad "80 缺 /healthz（供容器 healthcheck）"
grep -qE 'return\s+301\s+https://' "$NGINX_CONF" && ok "80 其余 301→https" || bad "80 缺 301→https"
grep -qE 'location\s*/\s*\{\s*return\s+301' "$NGINX_CONF" && ok "80 location / 强制 https" || bad "80 location / 未强制 https"
grep -qE '^\s*listen\s+443\s+ssl' "$NGINX_CONF" && ok "443 终结 SSL" || bad "443 未 listen 443 ssl（应仅 443 终结 TLS）"
grep -qE 'ssl_certificate\s+[^;]+server\.crt' "$NGINX_CONF" && ok "443 ssl_certificate 指向 /etc/nginx/certs/server.crt" || bad "443 未指向 server.crt"
grep -qE 'ssl_certificate_key\s+[^;]+server\.key' "$NGINX_CONF" && ok "443 ssl_certificate_key 指向 server.key" || bad "443 未指向 server.key"
grep -qE 'location\s+/api/' "$NGINX_CONF" && ok "443 同源代理 /api" || bad "443 缺 /api/ 同源代理"
grep -qE 'proxy_pass\s+http://api:8000' "$NGINX_CONF" && ok "443 /api/* → api:8000（同源）" || bad "443 /api/* 未代理到 api:8000"
grep -qE 'location\s*=\s*/api/metrics' "$NGINX_CONF" && ok "443 /api/metrics 内网化(404)" || bad "443 /api/metrics 未内网化（公网应 404）"

# ===================== [B] 运行时：仅 443 终结、80 不供应用 =====================
echo "[B] 运行时 80/443 行为"
HTTP_ROOT="http://$HOST:$HTTP_PORT"
HTTPS_ROOT="https://$HOST:$HTTPS_PORT"

# 80 healthz → 200
code_80_hz=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$HTTP_ROOT/healthz" || echo '000')
[ "$code_80_hz" = "200" ] && ok "80 /healthz → 200（容器 healthcheck）" || bad "80 /healthz → ${code_80_hz}（期望 200）"

# 80 其它路径 → 301（强制 https）
code_80_root=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$HTTP_ROOT/" || echo '000')
[ "$code_80_root" = "301" ] || [ "$code_80_root" = "302" ] || [ "$code_80_root" = "308" ] \
  && ok "80 / → ${code_80_root}（301 强升 https）" || bad "80 / → ${code_80_root}（期望 3xx 重定向）"
code_80_api=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$HTTP_ROOT/api/healthz" || echo '000')
[ "$code_80_api" = "301" ] || [ "$code_80_api" = "302" ] || [ "$code_80_api" = "308" ] \
  && ok "80 /api/healthz → ${code_80_api}（301 强升 https）" || bad "80 /api/healthz → ${code_80_api}（期望 3xx）"

# 443 根路径 → 前端（200/304 视为正常）；443 /api/healthz → api 200
code_443_root=$(curl -ks -o /dev/null -w '%{http_code}' --max-time 10 "$HTTPS_ROOT/" || echo '000')
[ "$code_443_root" = "200" ] || [ "$code_443_root" = "304" ] \
  && ok "443 / → ${code_443_root}（前端可达）" || warn "443 / → ${code_443_root}（预发布可能为前端占位）"
code_443_api=$(curl -ks -o /dev/null -w '%{http_code}' --max-time 10 "$HTTPS_ROOT/api/healthz" || echo '000')
[ "$code_443_api" = "200" ] && ok "443 /api/healthz → 200（同源 api 可达）" || bad "443 /api/healthz → ${code_443_api}（期望 200）"

# ===================== [C] 证书链有效性 + SAN 匹配 =====================
echo "[C] 证书链与 SAN（期望域名：$EXPECT_DOMAIN）"
command -v "$OPENSSL" >/dev/null 2>&1 || die "未找到 openssl（$OPENSSL）。"

TMPCERT="$(mktemp -d)/leaf.pem"
# 用 openssl s_client 抓取 peer 证书（-servername 用于 SNI 匹配正式域名）
if ! "$OPENSSL" s_client -connect "$HOST:$HTTPS_PORT" -servername "$EXPECT_DOMAIN" -showcerts </dev/null 2>"$TMPCERT.err" >"$TMPCERT.chain"; then
  bad "TLS 握手失败（$HOST:$HTTPS_PORT），无法抓取证书。"
  echo "  >> s_client 输出：$(tail -n 3 "$TMPCERT.err" 2>/dev/null | tr '\n' ' ')"
else
  # 提取第一张证书（leaf）到 leaf.pem
  awk '/BEGIN CERTIFICATE/{f=1} f{print} /END CERTIFICATE/{if(++n==1) exit}' "$TMPCERT.chain" > "$TMPCERT" 2>/dev/null
  if [ ! -s "$TMPCERT" ]; then
    bad "未能从 TLS 会话提取 leaf 证书。"
  else
    # 有效期
    nb="$(openssl x509 -in "$TMPCERT" -noout -startdate 2>/dev/null | sed 's/notBefore=//')"
    na="$(openssl x509 -in "$TMPCERT" -noout -enddate 2>/dev/null | sed 's/notAfter=//')"
    now_epoch=$(date +%s)
    nb_epoch="$(date -d "$nb" +%s 2>/dev/null || echo 0)"
    na_epoch="$(date -d "$na" +%s 2>/dev/null || echo 0)"
    if [ "$nb_epoch" -le "$now_epoch" ] && [ "$now_epoch" -le "$na_epoch" ]; then
      ok "leaf 在有效期内（${nb} ~ ${na}）"
    else
      bad "leaf 已过期/未生效（${nb} ~ ${na}）"
    fi
    # 非自签
    subj="$(openssl x509 -in "$TMPCERT" -noout -subject 2>/dev/null)"
    iss="$(openssl x509 -in "$TMPCERT" -noout -issuer 2>/dev/null)"
    if [ "$subj" = "$iss" ]; then
      bad "leaf 为【自签名】（Subject==Issuer）→ 正式生产【不可用】。受信 CA 未接入（BLOCKED_EXTERNAL）。"
    else
      ok "leaf 非自签（Subject≠Issuer）"
    fi
    # 链验证
    if [ -n "$CACERT" ] && [ -f "$CACERT" ]; then
      if openssl verify -purpose sslserver -CAfile "$CACERT" "$TMPCERT" >/tmp/verify.out 2>&1; then
        ok "证书链验证通过（-CAfile 受信根）：$(cat /tmp/verify.out)"
      else
        bad "证书链验证失败：$(cat /tmp/verify.out)"
      fi
    elif [ "$REQUIRE_TRUSTED" = "1" ]; then
      # 用系统信任库验证
      if openssl verify -purpose sslserver "$TMPCERT" >/tmp/verify.out 2>&1; then
        ok "证书链验证通过（系统信任库）：$(cat /tmp/verify.out)"
      else
        bad "证书链验证失败（系统信任库）：$(cat /tmp/verify.out)"
      fi
    else
      warn "未提供 --cacert 且未 --require-trusted，链信任【未验证】；正式验收请传 --cacert 受信根。"
    fi
    # SAN 匹配
    sans="$(openssl x509 -in "$TMPCERT" -noout -ext subjectAltName 2>/dev/null | tr ',' '\n' | sed -n 's/^[[:space:]]*DNS://p' | tr -d '\r' | sort -u)"
    if echo "$sans" | grep -qx "$EXPECT_DOMAIN"; then
      ok "SAN 含正式域名：$EXPECT_DOMAIN"
    else
      bad "SAN 未含正式域名：$EXPECT_DOMAIN（现存 SAN：$(echo "$sans" | paste -sd, -)）"
    fi
    # server_name 一致性核对
    srvs="$(grep -E 'server_name\s+[^;]+;' "$NGINX_CONF" | sed -E 's/.*server_name\s+([^;]+);.*/\1/' | tr -d ' ')"
    echo "  [info] nginx prod.conf server_name：${srvs:-<未设置>}"
  fi
fi
rm -rf "$(dirname "$TMPCERT")" 2>/dev/null || true

echo ""
if [ "$FAIL" -ne 0 ]; then
  printf '\033[1;31m受信 TLS 验收：%d 项未通过 / 共 %d 项。\033[0m\n' "$FAIL" "$((PASS+FAIL))"
  exit 1
fi
printf '\033[1;32m受信 TLS 验收通过（%d 项）。\033[0m\n' "$PASS"
