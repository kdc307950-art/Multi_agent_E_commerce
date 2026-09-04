# 生产部署记录（正式交付 · 基于 t1/t5/t6 真实结果回填）

> 本记录由 t4 模板回填为正式交付物，值来自 **t1（部署基线）/ t5（动态安全验收）/ t6（DR 演练）真实产物**。
> 未实测项一律如实标注，不声称达标。生成时间戳：`20260904-030903`。

---

## 0. 记录标识
- 记录 ID：`PROD_DEPLOY-20260904-030903`
- 部署目标环境：`preview`（preview 栈）；`production` 未部署（本阶段为 preview 上线前置）
- 部署时间戳 `<ts>`：`20260904-030903`（复盘时间；栈拉起时间为 2026-09-04 约 02:5x，见 t1/rollback.log）
- 执行人 / 复核人：`deploy-engineer` / `security-auditor`
- 数据来源：t1 部署基线（compose ps、健康检查、门控）+ t5 动态验收 + t6 DR 演练

## 1. 拓扑与暴露面（t1 实测）
| 项 | 值 | 依据 |
|---|----|------|
| Compose 项目名 | `after-sales-preview` | t1 |
| 暴露面 | 仅 `nginx` 发布宿主 **80/443**（`PortBindings {"80/tcp","443/tcp"}`）；其余容器 `PortBindings={}` 无宿主端口发布 | t1 |
| 私网（internal） | `postgres` `redis` `api` `worker` `frontend` `migrate` | t1 |
| nginx 代理 | `/api/* → api:8000`；其余 → `frontend:3000`；80 → 301 443 | t1 |
| 观测栈 | `after-sales-observability`（prometheus/grafana/loki/promtail/langfuse-postgres/langfuse-redis healthy；obs 网 internal） | t1 |

## 2. 组件版本
| 组件 | 镜像/版本 | 依据 |
|------|-----------|------|
| PostgreSQL | `postgres:17-alpine`（DB=`langgraph`，owner=`migrator`；运行角色 `app_runtime` NOBYPASSRLS 分离） | t1/t5 |
| Redis | `redis:7-alpine`（requirepass） | t1 |
| API | `Dockerfile` → `python -m src.*`（`api:8000`） | t1 |
| Worker | `Dockerfile` → `celery -A src.tasks.worker worker` | t1 |
| Frontend | `./frontend/Dockerfile`（Next.js 生产） | t1 |
| nginx | `nginx:1.27-alpine` | t1 |
| migrate | `Dockerfile` → `python -m src.infrastructure.migrate_cli`（一次性，exit=0） | t1 |
| **backup（周期备份，t12 新增）** | `postgres:17-alpine` sidecar，`command=sh /app/backup.sh`，每 `BACKUP_INTERVAL_SECONDS=900`（15min）`pg_dump -Fc`，`BACKUP_KEEP=8`，宿主持久 `./data/preview-backups:/backups` | t12 |
| 观测 | `prometheus:v2.53.0` / `grafana:11.1.0` / `loki:3.1.0` / `langfuse`（**v2.95.11，健康**）/ `promtail` / `langfuse-postgres` / `langfuse-redis`（观测栈 7 组件 healthy） | t1/t8 |

## 3. Git 引用与构建（t10 git archive 固定 mtime 上下文 冷构建复验 · tag baseline-prod-4）
- 发布 tag：**`baseline-prod-4`**（`git rev-parse baseline-prod-4` = HEAD）
- **git commit（完整 / 短）**：`03819a8dec94af079a039685a168e7c081dcbaa9` / `03819a8`（branch `main`；message：`fix(reproducibility): build_from_worktree 改用 git archive 固定 mtime 物化构建上下文`）
- **t10 做法（构建上下文）**：`deploy/scripts/common.sh::build_from_worktree` 由 `git worktree add` 改为 **`git archive <ref> | tar -x -C <dir>`**（导出的文件 mtime=**提交时间**，与克隆/检出时间无关），物化该 ref 已提交文件为构建上下文；保留 `repo_is_dirty` 拒绝逻辑；清理改为 `rm -rf` 临时目录。Dockerfile/requirements/lock 沿用 t9 的钉版（base 钉 digest + 依赖全 `==`/lock + SOURCE_DATE_EPOCH）。
- 构建方式：**git archive 上下文** + `compose build --no-cache --provenance=false --sbom=false api` + `SOURCE_DATE_EPOCH=1704067200`（build env）。
- **环境配置版本（DEPLOY_CONFIG_VERSION）**：`0.1.0`（`src/config.py` 默认；未显式覆盖）。
- **baseline-prod-4 冷构建 api 镜像 digest（实测 `docker image inspect .Id`）**：两处独立 git-archive 上下文（A/B，源文件字节/哈希/mtime 完全一致）Build A `sha256:8d5149f9b83539d8423859307ae9cf9ca17915333485b98a489d49b03d797ce3`；Build B `sha256:cad78f1d094d5ad833f61cb4011ac4e726089f152fdccfdba7e3d7b3bc96ea35`。两 build `Created` 均 = `2024-01-01T00:00:00Z`（SOURCE_DATE_EPOCH 生效）。
- **构建时间**：冷构建 `Created=2024-01-01T00:00:00Z`（`SOURCE_DATE_EPOCH=1704067200` 固定；实际构建于本会话）。运行镜像仍为 t1 旧代码（`9cd9238b…` 等）运行态，非 baseline-prod-4 重建。

### 可复现性检查（baseline-prod-4 同 ref **冷构建** `--no-cache` + git archive 上下文，t10 实测）
- **⚠ 仍未字节级一致，且决定性证明为 BuildKit 级非确定**：
  - `git archive baseline-prod-4` 两处独立上下文（A/B，`Dockerfile`/`requirements-lock.txt`/`pyproject.toml` 的字节 SHA、mtime、属性**完全一致**，mtime=提交时间）+ `SOURCE_DATE_EPOCH=1704067200` 分别 `compose build --no-cache --provenance=false --sbom=false api`：Build A `sha256:8d5149f9…`，Build B `sha256:cad78f1d…`，**不一致**；层 5-8（COPY requirements-lock.txt / RUN pip / COPY src / COPY pyproject.toml）不同。
  - **决定性证据**：对**同一个**上下文目录做**两次** `--no-cache` 冷构建（`SOURCE_DATE_EPOCH` 固定、`Created` 均=2024-01-01），两次 digest 仍不同（`0b14613d8fffd9487950f6894da8b77e3f9621419998fb0bd9056ef31c62ffc7` vs `6ac57cdfa5ae22a3b6c8e04798e04f994fd626732bd91d7f0ee61d5428c03597`）→ **非确定来自 Docker BuildKit（本 Docker Desktop buildx v0.35）构建过程本身**，与上下文/源码/mtime/base/依赖无关。
  - 即便额外加 `.dockerignore` 过滤掉中文文档等非构建文件后（上下文最小、无中文文件名），冷构建层 5-8 仍漂移。
- **根因**：本环境 buildkit **不保证冷构建层 DiffID 字节稳定**（即使输入完全相同）；`SOURCE_DATE_EPOCH`（env）只归一化 config `created`，未归一化层 tar；`--reproducible`（真正归一化层时间戳/内容）在本 buildx **不支持**（`unknown flag`，实测）。→ **仅靠源码/上下文/base/依赖钉版无法在本环境达成冷构建字节级一致**。
- **结论**：
  - **内容与配置已确定**：base 钉 digest + 依赖全 `==`/lock + `git archive` 固定 mtime + `SOURCE_DATE_EPOCH` 固定 created + `--provenance=false --sbom=false`（均已落地）。
  - **deploy 管线（`--pull` + 层缓存，非 `--no-cache`）在 base+lock 钉住后缓存稳定 → 实际上线可复现**。
  - **字节级冷构建**一致需要**可复现的构建器**（支持 `--reproducible` 的 buildkit / kaniko / ko / `uv build --reproducible` + 固定时钟的 CI）。
- **建议**：切换到支持 `--reproducible` 的 buildkit（或 kaniko/ko / `uv pip compile`+哈希+固定时钟 CI）后，在固定时钟下重跑冷构建复验以给出字节级证据；当前保留 base 钉 digest + 依赖 lock + git archive + SOURCE_DATE_EPOCH + provenance=false（均为内容/配置确定性的正确措施）。
- 注：`baseline-prod-1/2/3` 的“两遍一致”均系**命中缓存**，非真正冷构建一致；t9/t10 冷构建（`--no-cache`）暴露的图层/构建器非确定为**真实残余风险**。

## 4. 环境与关键配置（t1 实测 + t5 交叉）
| 变量 | 值 |
|------|----|
| `ENV` | `preview` |
| `STORAGE_BACKEND` | `postgres` |
| `AUTH_BACKEND` | `real` |
| `AUTH_CREDENTIAL_HASH` | `argon2id`（默认；t5 接线，禁 sha256/md5 弱哈希） |
| `DEMO_SEED_ENABLED` | `false`（preview/production 禁用 seed_default；真实租户走受控迁移/运维脚本） |
| `LOGIN_RATE_LIMIT_STORE` / `LOGIN_RATE_LIMIT_WINDOW_SECONDS` / `LOGIN_ACCOUNT_RATE_LIMIT` / `LOGIN_IP_RATE_LIMIT` | `memory` / `300` / `5` / `30` |
| `LOGIN_BACKOFF_MAX_FAILURES` / `LOGIN_BACKOFF_BASE_SECONDS` / `LOGIN_BACKOFF_MAX_SECONDS` / `LOGIN_TRUSTED_PROXY_DEPTH` | `5` / `1.0` / `900` / `1` |
| `DEPLOY_CONFIG_VERSION` | `0.1.0`（发布记录配置版本） |
| `ENABLE_CORS` | `false`（API 无 `Access-Control-Allow-Origin`） |
| `LAUNCH_ALLOWED_TENANTS` | `TENANT-A,TENANT-B` |
| `LAUNCH_GATE_STRICT` / `LAUNCH_REQUIRE_APPROVAL` / `LAUNCH_FULL_AUDIT` / `LAUNCH_MANUAL_REVIEW` | `true` |
| `EXECUTION_MODE` / `EXECUTION_PROVIDER` | `shadow` / `mock` |
| `LLM_BACKEND` | `mock`（本栈可用并驱动到审批） |
| `HIGH_CONFIDENCE_MODELS` | `self-hosted-demo`（评测 `write_op_pass=true`，能力矩阵交集非空） |
| 密钥注入 | `deploy/.env.preview` 已生成（真实随机密钥，`check_secrets` 逻辑等效 PASS；**注明**：本记录为 t1 早期状态，当时 `AUTH_LOGIN_CREDENTIALS` 用 `issue_login_token` 的 sha256 hex、且键集与 seed_default 的 10 成员一致 —— 该口径已被 **t2/t5 新基线 superseded**：登录凭据必须为 argon2id/bcrypt PHC（`AUTH_CREDENTIAL_HASH`），且 preview/production 禁用 `seed_default`，首批真实租户走受控迁移/运维脚本。此处保留历史事实，仅作归档。 |

## 5. 健康检查/可达性（t1 实测）
| 项 | 结果 |
|---|------|
| `http://127.0.0.1/healthz`（nginx） | ok |
| `https://127.0.0.1/api/healthz` | 200 `{"status":"ok"}` |
| `/`（前端） | 200 |
| `/api/metrics` | 200 |
| API CORS | 无 `Access-Control-Allow-Origin`（ENABLE_CORS=false 同源） |
| 观测↔预览桥接 | Prometheus 经 `app-net`(=after-sales-preview_internal) 抓取 `api:8000/api/metrics`，target health=up、lastError 空 |

## 6. 仅 80/443 验证（**已达成（t8 干净快照）**）
- **preview 拓扑层面**：✅ nginx 仅发布宿主 80/443；postgres/redis/api/worker/frontend/migrate `PortBindings={}`（无宿主端口发布）；`preview.conf` 80 仅 healthz/301、443 ssl 终结。
- **宿主运行时层面**：✅ **已达成**（t8）：停 dev 栈（`multi_agent_e_commerce` down，保留卷）+ 停遗留进程（Windows python uvicorn 8000/8001、WSL redis-server 6379、PostgreSQL 55432），形成干净快照；**本项目宿主监听= nginx {80,443}**。**注明**：3100 为**无关遗留宿主进程**（非本项目服务，观察期留意）。
- 结论：**仅 80/443 达成**（本项目拓扑+宿主运行时）。

## 7. TLS 证书
- 证书路径：`deploy/secrets/certs/server.crt` / `server.key`（gitignored）
- CN / SAN：`CN=preview.local`；`SAN=preview.local,localhost,127.0.0.1`
- 类型：**自签名**（有效期 365 天，`python:3.12-slim` 容器生成）
- 生产受信 CA：`未配置` —— **本记录为自签名，仅内部预发布，不作为生产受信凭证**；生产须替换为受信 CA（t2 红线 RL-01 关联）。

## 8. 上线门控三道闸门（t1 实测）
| 闸门 | 结果 |
|------|------|
| 密钥检查（`check_secrets` 逻辑等效） | PASS（必需项非空/非占位、回调 HMAC 非默认、评测报告存在、ENV=preview 强制 strict、shadow+mock 合法、JWT 密钥非占位） |
| 严格上线门控（`verify_launch_gate.py --strict`） | PASS（0 违规；`launch_allowlist=[TENANT-A,B]`；`postgres`；`shadow`） |
| fail-closed 认证守卫（`verify_preview_fail_closed` 等效） | PASS（preview+mock 拒、production+mock 拒、preview+real+空JWT 拒、preview+real+JWT 允许构建；`FAIL_CLOSED_VERIFY: OK`） |

## 9. 回滚就绪
- `deploy/backups/rollback.log`：已有 `deploy ref=<拉起时 ref>`（t1）；当前 HEAD=`7941246`
- 变更前备份：`deploy/backups/` 下存在 `drill-pg-backup-*.dump`、`pre-rollback-20260904030354.dump`、`langgraph-rollback-goal-20260904030342.dump`（t6）
- 回滚入口：`rollback.sh` 等效（t6 实跑通过，见 `DR-20260904030354-rollback.md`）

## 10. 结论（如实）
- 部署是否成功：**是**（t1：全部 6 服务 healthy；`/api/healthz=200`；门控三道闸门 PASS）
- 是否达到 preview 验收：**达成（含 RPO）**。拓扑/健康/门控/观测桥接/仅 80/443/观测栈 全达标；**宿主"仅 80/443"已达成（t8）**；**观测栈 `langfuse` 已修复为 v2.95.11 且健康（t8）**；**RPO 达标（t12 新增 `backup` 服务每 15min `pg_dump -Fc`，保留 8，宿主持久 `./data/preview-backups`，校验 marker_in_restore=0、TENANT_A_in_restore=1、restore_RTO=1.9s → RPO 上界=15min 周期界定，≤15min 达标）**。真实 LLM（openai_compatible）+ 真实外部网关（live）链路、经 nginx 外部入口的端到端 HTTPS 流程**未实测**（t5 明示）。
- **重要缺陷（已修复）**：t5 发现高严重度 **FOUND-SOFTWARE-1**（`postgres_store.py` JSONB 重复 `json.loads`），导致审批通过后执行收尾崩溃、SSE 重放崩溃、`/api/audit` 查询崩溃 → 本应成功的执行被误标 FAILED→转人工。**t9 已在仓库修复并 commit（HEAD=`7941246`，新增 `_as_json`，5 处 JSONB 读取切换，+20/-5，未夹带 migrations.py）**，重建 api/worker 生效，E2E 通过（审批→`executed`、SSE 重放、`/api/audit` 正常、无重复执行）。**注：`src/infrastructure/migrations.py` 为先前会话改动仍未提交**。
- **放量/验收**：D3-D6（审批门控/SSE 重放/并发幂等/对账）已基于修复后版本 **t10 复跑全部 PASS**（真实 JWT+mock LLM）；RTO=1.668s 达标、无跨租户/重复/绕过/未审计写 PASS、RPO 达标（t12 周期界定）。**仍未实测/待办**：对账 mismatch 计数（后台）、多租户并发压力、应用侧 Langfuse trace 真实上报、告警指标增强（完成标准核对表已按三档标注处理）。
- 签名：`deploy-engineer` / `security-auditor`
