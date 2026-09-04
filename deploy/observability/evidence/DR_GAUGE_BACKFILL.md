# RPO/RTO gauge 回填说明（t8）—— 闭合 RpoExceeded / RtoExceeded 写路径缺口

> 结论：`deploy/observability/alert-rules.yml` 的 `RpoExceeded`（`drill_rpo_seconds{component="pg_backup"}>900`）
> 与 `RtoExceeded`（`drill_rto_seconds{component="pg_backup"}>3600`）此前**永不触发**，因为灾备脚本只把实测
> RPO/RTO 写入 `deploy/drills/records/*.md|*.json`，**从未回填**对应的 Prometheus gauge。
> 本任务补齐该写路径：DR 脚本实测后把 RPO/RTO 回填为 gauge，经现有 `api:8000/api/metrics` 抓取即可采集，
> 使两条规则从"恒不触发"变为"可触发"。

---

## 1. 触发路径（从"恒不触发"→"可触发"）

```
恢复演练/备份实测得到 RPO/RTO
   │  restore_drill.sh / backup_encrypted.sh（宿主）
   │  调用 dr_backfill_gauges（common.sh）→ deploy/scripts/drill_metric_exporter.py
   │  write
   ▼
deploy/drills/metrics/drill_gauges.json      （host 侧状态文件，只存 rpo/rto/component/backup_file/updated_at）
   │  由 docker-compose.preview.yml api 服务只读挂载 ./deploy/drills/metrics:/app/evidence/dr_metrics:ro
   ▼
api 容器 /app/evidence/dr_metrics/drill_gauges.json
   │  GET /api/metrics（src/api/routes.py::metrics → _load_drill_gauges()）
   │  把 rpo/rto 合并进进程内 registry：get_metrics().set("drill_rpo_seconds", ..., ("component",), {"component":"pg_backup"})
   ▼
/api/metrics 文本：            # TYPE drill_rpo_seconds gauge
                              drill_rpo_seconds{component="pg_backup"} 0.815516
   │  Prometheus scrape job_name=api → api:8000/api/metrics（deploy/observability/prometheus.yml）
   ▼
Prometheus 时序表 drill_rpo_seconds{component="pg_backup"}
   │  alert-rules.yml rule: drill_rpo_seconds{component="pg_backup"} > 900  (for: 1m)
   ▼
RpoExceeded / RtoExceeded 触发（当实测超阈值）
```

- **规则表达式未改**（与 t3 对齐）；仅新增**写路径**。
- **标签有界**：仅 `component="pg_backup"`；`metrics._check_labels` 拒绝任何高基数/敏感维度。
- **增量、非关键路径**：状态文件缺失/非法时 `_load_drill_gauges` 直接返回，gauge 缺省即不暴露，`/api/metrics` 不受影响；RPO/RTO 的 `deploy/drills/records/*.md` 证据**仍保留**。

## 2. 配置与注入（preview 受限环境所需）

| 项 | 值 | 说明 |
|---|---|---|
| 环境变量 `DRILL_GAUGE_STATE` | `/app/evidence/dr_metrics/drill_gauges.json` | api 容器内状态文件路径（compose preview.yml 已注入） |
| 挂载 | `./deploy/drills/metrics:/app/evidence/dr_metrics:ro` | host 侧状态目录只读挂载进 api 容器 |
| 状态文件 | `deploy/drills/metrics/drill_gauges.json` | 由 DR 脚本（`drill_metric_exporter.py`）写 host 侧；宿主机与绑定挂载同源 |
| 脚本 python | `python3`（`PYTHON` 可覆盖） | `dr_backfill_gauges` 用它调用 exporter；无 python 则跳过并告警（不中断演练） |
| 前置 | `docker compose up -d`（api 服务）已含挂载 | 状态目录不存在时 Docker 自动建空目录，演练后即有内容 |

> 宿主机与 api 容器必须指向**同一份** host 状态文件（compose bind mount 即保证）。DR 脚本跑在宿主机，
> 写入 `$DEPLOY_DIR/drills/metrics/drill_gauges.json`，api 容器读到的正是同一份。

## 3. 脚本改动清单

- 新增 `deploy/scripts/drill_metric_exporter.py`（纯 stdlib：`write`/`render`/`verify`）。
- `deploy/scripts/common.sh`：新增 `dr_backfill_gauges()`（调用 exporter，失败仅告警）。
- `deploy/scripts/restore_drill.sh`：实测得到 RPO/RTO 后（步骤 10 之后）`dr_backfill_gauges "$RPO_MEASURED" "$RTO" "$(basename "$ENC")"`。
- `deploy/scripts/backup_encrypted.sh`：备份完成后 `dr_backfill_gauges "${BACKUP_INTERVAL_SECONDS:-900}" "" "$(basename "$ENC")"`（回填 RPO=备份周期上界；RTO 由演练/不覆盖）。
- `deploy/drills/verify_dr_compose.sh`：结尾 `render`/`verify` 复核已回填的 gauge。
- `src/api/routes.py`：`/api/metrics` 渲染前调用 `_load_drill_gauges()` 合并状态文件（`import os` 已加）。
- `docker-compose.preview.yml`：api 服务注入 `DRILL_GAUGE_STATE` 并挂载状态目录。

## 4. 实测回填证据（本机 WSL 恢复演练，真实测量）

- 状态文件：`deploy/drills/metrics/drill_gauges.json`
  ```json
  {"component":"pg_backup","rpo_seconds":0.815516,"backup_file":"langgraph-dr-verify.dump.enc",
   "updated_at":"2026-09-04T05:53:05.777608+00:00","rto_seconds":0.094207}
  ```
- 渲染（与 alert 表达式对齐的 Prometheus 文本）：
  ```
  # TYPE drill_rpo_seconds gauge
  drill_rpo_seconds{component="pg_backup"} 0.815516
  # TYPE drill_rto_seconds gauge
  drill_rto_seconds{component="pg_backup"} 0.094207
  ```
- 阈值校验：`rpo 0.815516(<=900) PASS`、`rto 0.094207(<=3600) PASS`（本次实测未超阈值，故规则不触发——正确；当实测 >900s / >3600s 即触发）。
- 证据来源：`deploy/drills/records/drill-pg-encrypted-restore.json`（`rto_restore_seconds=0.094207`、`rpo_measured_seconds=0.815516`）+ `drill-pg-encrypted-restore.md`。

> obs-engineer t7 已用合成样本（`eval_alert_rules.py`：rpo=1000/rto=4000 → 命中）证明**表达式可触发**；
> t8 补齐的是**写路径**（实测值真实写入 gauge 并被 /api/metrics 采集），二者汇合后方可真正告警。

## 5. 待办 / 边界
- 以上在**本机 WSL 演练 + 独立 exporter** 验证通过；api 侧合并由 `py_compile` 与逻辑等价验证。
  **完整运行时**（api 容器读取挂载状态文件 → /api/metrics → Prometheus 抓取 → 告警）需在 preview 服务器
  `docker compose up -d` 后跑 `deploy/drills/verify_dr_compose.sh` 复验（本轮未动容器）。
- 若生产改用 Pushgateway，可让 `drill_metric_exporter.py` 加 `--push-url` 推送；当前方案复用现有 api scrape，无需新增服务。
