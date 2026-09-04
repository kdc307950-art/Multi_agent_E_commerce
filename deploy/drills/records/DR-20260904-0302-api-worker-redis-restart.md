# 预发布演练记录 — API / worker / Redis 重启（DR-20260904-0302）

- 演练编号：DR-20260904-0302
- 环境：preview（after-sales-preview）
- 应用 git 引用：251f430
- 执行人 / 复核人：drill-runner / security-auditor
- 开始时间 / 结束时间：2026-09-04 03:02:49（drill-restart-services.json timestamp）

| # | 演练项 | 证据ID | 结果 | 备注 |
|---|--------|--------|------|------|
| D1 | API 重启 | api_healthz_200=True | True | 重启后 /api/healthz 200 |
| D1 | worker 重启 | worker_running=True | True | 重启后仍在运行 |
| D1 | Redis 重启 | redis_restart_pg_ready=True | True | PG 数据面仍就绪（数据面不依赖 Redis） |

## 结论
- 是否达到预发布验收：True（pass=3，fail=0；结构化：drill-restart-services.json）
- 签名：drill-runner / security-auditor
