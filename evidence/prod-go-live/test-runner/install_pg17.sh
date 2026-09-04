#!/usr/bin/env bash
set -euo pipefail
# 安装 postgresql-client-17（匹配 DB 版本），使 WSL 的 pg_dump/pg_restore 与 v17 服务端兼容。
export DEBIAN_FRONTEND=noninteractive
apt-get update >/tmp/apt_fix.log 2>&1 || true
apt-get install -y curl ca-certificates gnupg lsb-release >/tmp/apt_fix2.log 2>&1 || true
CODENAME=$(. /etc/os-release && echo "$VERSION_CODENAME")
echo "codename=$CODENAME"
curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor -o /usr/share/keyrings/pgdg.gpg 2>/tmp/pgkey.log || true
echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] http://apt.postgresql.org/pub/repos/apt $CODENAME-pgdg main" \
  > /etc/apt/sources.list.d/pgdg.list
apt-get update >/tmp/apt_update.log 2>&1 || true
apt-get install -y postgresql-client-17 >/tmp/apt_pg17.log 2>&1 || true
echo "--- pg_dump version ---"
pg_dump --version 2>&1 || echo "pg_dump NOT available"
echo "--- log tail ---"
tail -n 5 /tmp/apt_pg17.log 2>/dev/null || true
