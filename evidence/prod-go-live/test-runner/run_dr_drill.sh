#!/usr/bin/env bash
# run_dr_drill.sh —— 加密备份恢复演练执行入口（一次性独立 Postgres，direct-DSN 模式）。
# 密钥从同目录 .backup_key 读取（由 PowerShell 生成并写入，绝不出现在本脚本/日志）。
set -uo pipefail
REPO="/mnt/d/software/PythonProject1/PythonProject/Multi_agent_E_commerce"
KEY_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.backup_key"
[ -f "$KEY_FILE" ] || { echo "缺少密钥文件 $KEY_FILE"; exit 1; }
# 前置 v17 客户端（匹配 postgres:17 服务端），使 pg_dump/pg_restore 版本一致。
export PATH="/usr/lib/postgresql/17/bin:$PATH"
BACKUP_ENC_KEY="$(cat "$KEY_FILE")"
export BACKUP_ENC_KEY
export BACKUP_DB_URL="postgresql://backup_role:drill_backup_pw_QWE456@127.0.0.1:56742/langgraph_drill"
export REMOTE_BACKUP_DIR="$REPO/data/prod-backups"
export BACKUP_KEEP=8
cd "$REPO"
echo "=== STEP 1: encrypted backup (backup_role, aes-256-cbc) ==="
bash deploy/scripts/backup_encrypted.sh "$@"
