#!/usr/bin/env bash
set +e
echo "== ls /usr/lib/postgresql =="; ls /usr/lib/postgresql/ 2>&1
echo "== which pg_dump* =="; ls -la /usr/bin/pg_dump* 2>&1
echo "== v17 binary? =="; ls /usr/lib/postgresql/17/bin/pg_dump /usr/lib/postgresql/17/bin/pg_restore 2>&1
echo "== direct v17 version =="; /usr/lib/postgresql/17/bin/pg_dump --version 2>&1
echo "== pgdg sources =="; cat /etc/apt/sources.list.d/pgdg.list 2>&1
echo "== apt update tail =="; tail -n 8 /tmp/apt_update.log 2>&1
echo "== apt_pg17 log head =="; head -n 20 /tmp/apt_pg17.log 2>&1
