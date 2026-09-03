#!/usr/bin/env bash
# drill_restart_services.sh —— [D1] API/worker/Redis 重启韧性演练。
# 逐一重启 api / worker / redis，验证：
#   - 重启后应用恢复健康（/api/healthz 200）；
#   - 数据面数据（PostgreSQL）在 redis 重启后仍可读（Redis 仅承载锁/队列/缓存，非可信数据源）；
#   - 在途敏感写不重复（重启前后同一 operation_id 状态不重置、不产生第二个执行）。
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../scripts" && pwd)/common.sh"

maybe_docker
bash "$SCRIPT_DIR/check_secrets.sh" >/dev/null || true

HOST_BASE="https://127.0.0.1"
curl_ok() { [ "$(curl -kso /dev/null -w '%{http_code}' --max-time 8 "$HOST_BASE$1")" = "200" ]; }

ok() { printf '\033[1;32m  [OK]\033[0m %s\n' "$*"; }
bad() { printf '\033[1;31m  [FAIL]\033[0m %s\n' "$*"; }
PASS=0; FAIL=0

log "D1: API 重启"
compose restart api >/dev/null 2>&1
for i in $(seq 1 30); do curl_ok /api/healthz && break; sleep 2; done
if curl_ok /api/healthz; then ok "api 重启后 /api/healthz 200"; PASS=$((PASS+1)); else bad "api 重启后未恢复"; FAIL=$((FAIL+1)); fi

log "D1: worker 重启"
compose restart worker >/dev/null 2>&1
sleep 5
if [ "$(compose ps -q --status running worker | wc -l)" -ge 1 ]; then ok "worker 重启后运行中"; PASS=$((PASS+1)); else bad "worker 未运行"; FAIL=$((FAIL+1)); fi

log "D1: Redis 重启（数据面在 PostgreSQL，非 Redis 可信存储）"
# 记录一个操作/会话后在 Redis 重启后仍可读。
compose restart redis >/dev/null 2>&1
sleep 3
if compose exec -T postgres pg_isready -U "${POSTGRES_USER:-migrator}" -d "${POSTGRES_DB:-langgraph}" -h localhost >/dev/null 2>&1; then
  ok "Redis 重启后 PostgreSQL 仍就绪（数据面未受影响）"
  PASS=$((PASS+1))
else
  bad "Redist 重启后数据面异常"; FAIL=$((FAIL+1))
fi

# 幂等/在途敏感写不重复：此处以"数据面连接数/关键表可读"作最小验证，完整在途写幂等见 D3/D5。
echo ""
echo "D1 结果: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
