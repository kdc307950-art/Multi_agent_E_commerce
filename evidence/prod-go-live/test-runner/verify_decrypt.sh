#!/usr/bin/env bash
set -uo pipefail
REPO="/mnt/d/software/PythonProject1/PythonProject/Multi_agent_E_commerce"
KEY_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.backup_key"
export PATH="/usr/lib/postgresql/17/bin:$PATH"
BACKUP_ENC_KEY="$(cat "$KEY_FILE")"
export BACKUP_ENC_KEY
ENC="$REPO/deploy/backups/langgraph-20260904142219.dump.enc"
TMP=$(mktemp "$REPO/deploy/backups/.verify-XXXXXX.dump")
echo "== decrypt to $TMP =="
openssl enc -d -aes-256-cbc -pbkdf2 -salt -pass env:BACKUP_ENC_KEY < "$ENC" > "$TMP" && echo "DECRYPT_OK" || echo "DECRYPT_FAIL"
echo "== first 8 bytes of decrypted dump =="
head -c 8 "$TMP" | xxd | head -1
echo "== pg_restore --list (archive integrity) =="
pg_restore --list "$TMP" 2>&1 | head -n 5
echo "== list tally (objects) =="
pg_restore --list "$TMP" 2>/dev/null | grep -c . || true
rm -f "$TMP"
