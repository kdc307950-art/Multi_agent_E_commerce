# BASELINE_COMMIT_SCOPE — 忠实发布基线 commit 范围（应提交 / 绝不可提交）

> 角色：deploy-engineer（T7）· 关联：t1（基线）/ t2（迁移修复）
> 发布基线：`release/v1.0.0-rc1` → `cd743d3`（tree `9669ce25`）。忠实基线 = 7941246(已测功能) · 3268a1c(可复现构建) · 696444a(migrations 复合外键修复) · 8ddca48a(接受运行面+部署配置) · cd743d3(BUSINESS_DATA_BACKEND)。

## 一、已纳入基线（应提交，已在 cd743d3）
这些是**接受并验收通过**的运行面/部署配置，构成忠实基线，应全部纳入 git：

| 类别 | 内容 |
|------|------|
| `src/**` | 运行码：api/worker/engine/store(postgres/sqlite)/execution(含 sandbox_gateway.py、sandbox_faults.py)/llm(含 circuit_breaker.py)/retrieval/tools(含 data_source.py、postgres_data_source.py)/auth/observability/graph/infrastructure(含 migrations.py，含 shipping_events 复合外键修复)/main.py 等 |
| `tests/**` | 验收/回归测试：sandbox_*、concurrency_stress、rls_bypass、recovery_consistency、fault_injection、callback_concurrency、data_source_contract、security_regressions、observability 等 |
| `scripts/**` | 验收/压测/演练脚本：concurrency_stress.py、sandbox_concurrency_stress.py、pg_rls_bypass_probe.py、recovery_consistency_drill.py、create_bootstrapped_tenants.py、hash_login_credentials.py、evaluate_models.py 等 |
| `deploy/**` | 部署配置：`docker-compose.prod.yml`、`deploy/.env.preview.example`、`deploy/.env.production.example`、`deploy/nginx/prod.conf`、`deploy/nginx/preview.conf`、`deploy/scripts/*.sh`、`deploy/drills/*`、`deploy/records/*`、`deploy/observability/*`、`deploy/DR_KEY_MANAGEMENT.md`、`deploy/EGRESS_POLICY.md`、`deploy/OPS_RUNBOOK.md` 等 |
| `docker-compose.*.yml` | preview / prod / observability / dev compose |
| `Dockerfile`、`requirements.txt`、`requirements-lock.txt`、`pyproject.toml`、`frontend/**` | 构建/依赖/前端 |
| `evidence/**` | 验收证据文档/JSON/报告（含 `evidence/prod-go-live/deploy-engineer/*`：DEPLOY_BASELINE.md、IMAGE_DIGESTS.json、INDEPENDENCE.json、MIGRATE_VERIFY.md、PROD_STACK_HEALTH.md、MIGRATE_FAILURE.log、BASELINE_COMMIT_SCOPE.md；release-manager/security-auditor/test-runner/observability/acceptance 的证据） |
| `.gitignore`、`AGENTS.md`、`README.md`、`verify_*.py` | 宪法/文档 |

> 说明：`evidence/` 是验收/部署的证据链，应随基线入库以备可追溯；`git-baseline.txt` 为 git 转储（诊断用）。

## 二、绝不可提交（.gitignore 已拦截，严禁强加）
这些含**真实机密 / 运行时数据 / 缓存 / 二进制转储**，绝不入仓库：

| 类别 | 路径 / 模式 |
|------|------|
| **真实密钥文件** | `deploy/.env.production`、`deploy/.env.preview`、`.env`、`.env.local` |
| **证书私钥** | `deploy/secrets/certs/`（含 server.key/server.crt；**生产受信证书链也需经密钥管理/Secret 注入，不走仓库**） |
| **登录凭据** | `deploy/secrets/preview_login_credentials.txt` |
| **数据卷/库文件** | `data/`（`prod-postgres`、`prod-backups`、`prod-backups-offsite`、`preview-*`、`langfuse-postgres`、`grafana`、`minio`、`loki` 等）、`*.db`、`*.db-journal`、`*.sqlite`、`*.sqlite3`、`x.db`、`*_probe.db` |
| **缓存/字节码** | `__pycache__/`、`*.py[cod]`、`.pytest_cache/`、`.mypy_cache/`、`.ruff_cache/` |
| **Python/Node 环境** | `.venv/`、`.accept-crewai-venv/`、`node_modules/`、`.next/`、`out/`、`dist/` |
| **编排临时产物** | `.agent-teams/`、`_probe_py/`、`_acceptance_tmp/`、`.pytest-tmp/`、`.pytest-tmp-sqlite-diag/`、`graphflow-out/` |
| **备份/转储/灾备状态** | `deploy/backups/`、`*.dump`、`*.dump.enc`、`*.dump.enc.sha256`、`*.manifest.csv`、`deploy/drills/records/*.dump*`、`deploy/drills/records/drill-pg-encrypted-restore.json`、`deploy/drills/metrics/`、`data/preview-backups-offsite/` |
| **日志/临时** | `*.log`、`*.tmp`、`tmp/` |

## 三、结论
- **忠实基线成立**：接受运行面 + 部署配置已提交（`cd743d3`），`.gitignore` 正确拦截机密/数据/缓存/转储。
- **另需干净复现**：镜像当前基于**工作树**构建（代码与基线一致）；要达到字节级可复现，需在 `release/v1.0.0-rc1` 上用 `git archive` 物化 clean-context + `docker build --no-cache` 重建并记录新 digest（见 DEPLOY_BASELINE.md §7 #2，属"待干净重做"项）。
- **红线保证**：`deploy/.env.production`（真实随机密钥）与 `data/`（生产/预发布卷）均被 `.gitignore` 拦截，未入基线——发布过程不泄露机密、不复用 preview 卷。
