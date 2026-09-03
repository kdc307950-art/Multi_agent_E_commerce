#!/usr/bin/env bash
# run_preview_drills.sh —— 预发布演练编排器（在目标 pre-release 服务器上执行）。
# 依次运行六项演练脚本，每项 PASS/FAIL 汇总后写入 deploy/drills/records/ 下的 JSON+Markdown 记录。
# 任一项失败即非零退出（fail-closed 闸门）。
#
# 用法：bash deploy/drills/run_preview_drills.sh   （在仓库根、具备 Docker 的服务器上执行）
# 依赖：deploy/scripts/common.sh、已启动的 preview 栈（docker-compose.preview.yml）、
#       deploy/scripts/healthcheck.sh、deploy/scripts/backup_db.sh、check_secrets.sh。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(dirname "$SCRIPT_DIR")"
REPO_ROOT="$(dirname "$DEPLOY_DIR")"
RECORDS_DIR="$SCRIPT_DIR/records"
mkdir -p "$RECORDS_DIR"

source "$DEPLOY_DIR/scripts/common.sh"

TS="$(date +%Y%m%d%H%M%S)"
SUMMARY="$RECORDS_DIR/preview-drills-$TS.json"
MD="$RECORDS_DIR/preview-drills-$TS.md"

maybe_docker
bash "$SCRIPT_DIR/drill_restart_services.sh"        && echo "D1= PASS" >>"$RECORDS_DIR/.run-$TS" || echo "D1= FAIL" >>"$RECORDS_DIR/.run-$TS"
bash "$SCRIPT_DIR/drill_pg_backup_restore.sh"       && echo "D2= PASS" >>"$RECORDS_DIR/.run-$TS" || echo "D2= FAIL" >>"$RECORDS_DIR/.run-$TS"
bash "$SCRIPT_DIR/drill_api.sh approval"            && echo "D3= PASS" >>"$RECORDS_DIR/.run-$TS" || echo "D3= FAIL" >>"$RECORDS_DIR/.run-$TS"
bash "$SCRIPT_DIR/drill_api.sh sse"                 && echo "D4= PASS" >>"$RECORDS_DIR/.run-$TS" || echo "D4= FAIL" >>"$RECORDS_DIR/.run-$TS"
bash "$SCRIPT_DIR/drill_api.sh concurrent"          && echo "D5= PASS" >>"$RECORDS_DIR/.run-$TS" || echo "D5= FAIL" >>"$RECORDS_DIR/.run-$TS"
bash "$SCRIPT_DIR/drill_api.sh reconcile"           && echo "D6= PASS" >>"$RECORDS_DIR/.run-$TS" || echo "D6= FAIL" >>"$RECORDS_DIR/.run-$TS"

# 汇总
{
  echo "# 预发布演练记录 $TS"
  echo ""
  echo "- 应用 git 引用：$(cd "$REPO_ROOT" && git rev-parse --short HEAD 2>/dev/null || echo 'unknown')"
  echo ""
  echo "| # | 演练项 | 结果 |"
  echo "|---|--------|------|"
} >"$MD"
PASS=0; FAIL=0
while read -r line; do
  printf '%s\n' "$line"
  echo "| $(echo "$line" | cut -d= -f1) | $(basename "$line" | cut -d= -f1) | $(echo "$line" | cut -d= -f2) |" >>"$MD"
  [ "$(echo "$line" | cut -d= -f2)" = "PASS" ] && PASS=$((PASS+1)) || FAIL=$((FAIL+1))
done <"$RECORDS_DIR/.run-$TS"
rm -f "$RECORDS_DIR/.run-$TS"

printf '{"scenario":"preview_drills","timestamp":"%s","passed":%d,"failed":%d}\n' \
  "$TS" "$PASS" "$FAIL" >"$SUMMARY"

echo ""
echo "=========================================================="
echo "预发布演练汇总（$TS）：PASS=$PASS FAIL=$FAIL（共 $((PASS+FAIL))）"
echo "  记录: $MD"
echo "  汇总: $SUMMARY"
echo "=========================================================="
[ "$FAIL" -eq 0 ]
