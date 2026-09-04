#!/usr/bin/env bash
# run_recover_drill.sh —— 加密备份恢复演练（direct-DSN 模式）：复用 restore_drill.sh。
set -uo pipefail
REPO="/mnt/d/software/PythonProject1/PythonProject/Multi_agent_E_commerce"
KEY_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.backup_key"
export PATH="/usr/lib/postgresql/17/bin:$PATH"
BACKUP_ENC_KEY="$(cat "$KEY_FILE")"
export BACKUP_ENC_KEY
export DR_RESTORE_DSN="postgresql://migrator:drill_super_pw_ZXC123@127.0.0.1:56742/langgraph_drill"
export DR_MARKER_TABLE="tenants"
ENC="$1"
cd "$REPO"
echo "==== restore_drill.sh $ENC ===="
bash deploy/scripts/restore_drill.sh "$ENC"
