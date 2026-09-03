#!/usr/bin/env bash
# healthcheck.sh —— preview 部署验收（对应验收口径）。
# 逐项核验：
#   [1] 服务器仅开放 80/443（无 5432/6379/8000/3000 等宿主监听）。
#   [2] API 可经 HTTPS 同源访问（/ 与 /api/openapi.json 均 200，且 API 响应不含 CORS 头）。
#   [3] 数据库/Redis 无宿主监听（无公网暴露）。
#   [4] Mock 认证 / 缺 JWT 密钥时应用 fail-closed（verify_fail_closed.sh）。
# 任一项失败即非零退出。
# 用法：bash deploy/scripts/healthcheck.sh
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

maybe_docker
bash "$SCRIPT_DIR/check_secrets.sh"

# 仅在宿主机执行一次（可用 CAP_NET_ADMIN/root 判定）。
HOST_BASE="https://127.0.0.1"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '\033[1;32m  [OK]\033[0m %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '\033[1;31m  [FAIL]\033[0m %s\n' "$1"; }
need_curl() { command -v curl >/dev/null 2>&1 || bad "缺少 curl，无法执行 HTTPS 验收"; }

# ---- [1] 宿主仅开放 80/443 ----
listening_ports() {
  if command -v ss >/dev/null 2>&1; then
    ss -tlnH | awk '{print $4}' | grep -oE ':[0-9]+$' | tr -d ':' | sort -u
  elif command -v netstat >/dev/null 2>&1; then
    netstat -tln | awk '{print $4}' | grep -oE ':[0-9]+$' | tr -d ':' | sort -u
  else
    echo "UNKNOWN"
  fi
}

echo "[1] 宿主监听端口（应为 {80,443} 子集）"
PORTS="$(listening_ports)"
if [ "$PORTS" = "UNKNOWN" ]; then
  bad "无法枚举宿主监听端口（缺 ss/netstat）。请手工核验 仅 80/443 开放。"
else
  echo "    监听端口：$PORTS"
  # 允许 80 与 443；若出现任何"内网业务端口"即视为违规公网暴露。
  for p in 5432 6379 8000 3000; do
    if echo "$PORTS" | grep -qx "$p"; then
      bad "宿主开放了内网业务端口 $p（应为仅 80/443，可能暴露 PostgreSQL/Redis/API/前端）。"
    fi
  done
  if ! echo "$PORTS" | grep -qx "443"; then
    bad "宿主未监听 443（HTTPS 不可达）。"
  else
    ok "宿主监听 443（HTTPS）。"
  fi
fi

echo "[2] HTTPS 同源访问"
need_curl
if command -v curl >/dev/null 2>&1; then
  CODE_ROOT=$(curl -kso /dev/null -w '%{http_code}' --max-time 8 "$HOST_BASE/")
  [ "$CODE_ROOT" = "200" ] && ok "前端根路径 HTTPS 200（$HOST_BASE/）" || bad "前端根路径 HTTPS 非 200（实际 $CODE_ROOT）"

  API_HEALTH=$(curl -kso /dev/null -w '%{http_code}' --max-time 8 "$HOST_BASE/api/healthz")
  [ "$API_HEALTH" = "200" ] && ok "API 经 HTTPS 同源可访问（/api/healthz 200）" || bad "API 同源访问失败（实际 $API_HEALTH）"

  # ENABLE_CORS=false：API 响应不应含 Access-Control-Allow-Origin。
  ACAO=$(curl -ksi --max-time 8 "$HOST_BASE/api/healthz" | grep -i '^access-control-allow-origin:' || true)
  if [ -z "$ACAO" ]; then
    ok "API 未返回 CORS 头（ENABLE_CORS=false 生效）。"
  else
    bad "API 返回 CORS 头（$ACAO）；应同源代理并关闭 CORS。"
  fi
fi

echo "[3] 数据库/Redis 无公网监听"
# 无宿主监听（[1] 已核验 5432/6379 不监听）；再以 compose port 确认业务服务未发布宿主端口。
if [ "$PORTS" != "UNKNOWN" ] && ! echo "$PORTS" | grep -qx "5432" && ! echo "$PORTS" | grep -qx "6379"; then
  ok "PostgreSQL(5432)/Redis(6379) 无宿主监听。"
else
  bad "PostgreSQL/Redis 疑似公网监听，请检查端口发布。"
fi
LEAK=""
for svc_port in "postgres:5432" "redis:6379" "api:8000" "frontend:3000"; do
  svc="${svc_port%%:*}"; sp="${svc_port##*:}"
  if compose port "$svc" "$sp" >/dev/null 2>&1; then
    LEAK="$LEAK $svc:$sp"
  fi
done
if [ -n "$LEAK" ]; then
  bad "检测到业务端口发布：$LEAK（preview 只应 nginx 发布 80/443）。"
else
  ok "业务服务（postgres/redis/api/frontend）均无宿主端口发布。"
fi

echo "[4] Mock / 缺 JWT 密钥 fail-closed"
bash "$SCRIPT_DIR/verify_fail_closed.sh" && ok "受限环境 fail-closed 验证通过。" || bad "受限环境 fail-closed 验证失败。"

echo ""
if [ "$FAIL" -ne 0 ]; then
  printf '\033[1;31m健康检查失败：%d 项未通过 / 共 %d 项。\033[0m\n' "$FAIL" "$((PASS+FAIL))"
  exit 1
fi
printf '\033[1;32m健康检查通过（%d 项）。\033[0m\n' "$PASS"
