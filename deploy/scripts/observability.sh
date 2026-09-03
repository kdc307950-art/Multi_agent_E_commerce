#!/usr/bin/env bash
# observability.sh —— 自托管可观测栈（Langfuse/Prometheus/Grafana/Loki/Promtail）启停与健康检查。
# 用法：
#   bash deploy/scripts/observability.sh up      # 启动（需先启动 preview 栈）
#   bash deploy/scripts/observability.sh down    # 停止
#   bash deploy/scripts/observability.sh health  # 健康检查
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

OBS_COMPOSE="$REPO_ROOT/docker-compose.observability.yml"
OBS_NAME="after-sales-observability"

login() { printf '\033[1;34m[obs]\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m  [OK]\033[0m %s\n' "$*"; }
bad()   { printf '\033[1;31m  [FAIL]\033[0m %s\n' "$*"; }

obs_compose() {
  docker compose --env-file "$ENV_FILE" -f "$OBS_COMPOSE" "$@"
}

case "${1:-}" in
  up)
    maybe_docker
    bash "$SCRIPT_DIR/check_secrets.sh" >/dev/null || true
    login "启动自托管可观测栈 $OBS_NAME"
    obs_compose up -d
    login "等待 prometheus/grafana/loki/langfuse healthy"
    for i in $(seq 1 30); do
      ready=1
      for svc in prometheus grafana loki langfuse; do
        if [ "$(obs_compose ps -q --status running "$svc" | wc -l)" -lt 1 ]; then ready=0; fi
      done
      [ "$ready" -eq 1 ] && break
      sleep 5
    done
    ok "可观测栈已启动。Grafana: http://<host>:3000, Prometheus: http://<host>:9090"
    ;;
  down)
    login "停止可观测栈 $OBS_NAME"
    obs_compose down
    ;;
  health)
    maybe_docker
    PASS=0; FAIL=0
    check() { [ "$(obs_compose ps -q --status running "$1" | wc -l)" -ge 1 ] && ok "$1 运行中" || bad "$1 未运行"; }
    check prometheus; check grafana; check loki; check langfuse
    # 抓取验证：应用指标端点是否可被 prometheus 容器命中（需 preview 栈在跑）。
    SCRAPE=$(docker run --rm --network "$OBS_NAME"_app-net alpine/curl:latest \
              -s -o /dev/null -w '%{http_code}' --max-time 5 http://api:8000/api/metrics 2>/dev/null || echo "unreachable")
    [ "$SCRAPE" = "200" ] && ok "prometheus 可抓取 /api/metrics (200)" \
                          || bad "无法从 app-net 抓取 /api/metrics（$SCRAPE）。请确认 preview 栈在跑。"
    [ "$FAIL" -eq 0 ] && { ok "可观测栈健康检查通过"; exit 0; } || exit 1
    ;;
  *)
    echo "用法: $0 {up|down|health}"; exit 2
    ;;
esac
