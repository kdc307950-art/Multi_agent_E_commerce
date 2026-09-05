# DEPLOY_BASELINE — 正式生产发布基线 + 独立生产栈 + 镜像 digest + 独立性与门控

> **角色**：deploy-engineer（prod-go-live 团队） · **任务**：T1
> **工作区**：`D:\software\PythonProject1\PythonProject\Multi_agent_E_commerce`
> **环境**：Docker 29.6.2 / Compose v5.3.1；`after-sales-preview` 栈（7 容器 healthy）保持运行、未受影响。
> **生成时间**：2026-09-04
> **诚实声明**：凡"真实受信 CA 证书 / 真实域名 / 真实 LLM 端点与评测 / 首批书面确认租户"等外部输入未获提供，
> 一律如实标注 **BLOCKED-需外部**；凡未实际执行的（如生产栈健康）如实标注"未达成/失败"，绝不伪造证据。
>
> **口径更新（rc3 · 依 T1.1 审计 `security-auditor/RELEASE_DOC_CONSISTENCY_AUDIT.md`）**：本文是 **T1（2026-09-04）建立/验证基线时的现场记录**。当前发布锚点 = **`release/v1.0.0-rc3`**（annotated tag 对象 `d1d2867` → commit **`3ccab5c`** == `HEAD`）；**rc1/rc2 为不可移动历史锚点**——rc1→`cd743d3`、rc2→`fa7c9a3`（tag 对象 `290b830`；`bca4861` 为其阶段一收口"父提交"，仅历史说明）。**运行面（镜像代码）基线 = `cde30fb`**（IMAGE_DIGESTS `release_baseline.commit`），与 rc3 tag 所在 commit **有意区分**（rc3 为 docs/tests/gitignore-only 收口）。文中凡 "HEAD=3268a1c / 全容器 health" 等均为**当时观测或论证**，非当前运行态证明。
>
> **rc4-candidate 更新记录（✨ RC4 · release-manager）**：当前权威发布锚点推进至 **`release/v1.0.0-rc4-candidate`**（自 `release/v1.0.0-rc3`→`3ccab5c` 之上叠加"阶段一 rc4-candidate 收口"提交，含 CrewAI 适配器/tools schema + 测试证据污染修复 + Compose 更新 + prod-go-live 证据文档）；`release/v1.0.0-rc3`→`3ccab5c` 为历史锚点。权威测试快照 **396 passed, 34 skipped, EXIT=0**（41.92s，`evidence/prod-go-live/test-runner/pytest_captain_baseline.log`；34 skipped = 33 项 PG 无 `DATABASE_URL` + 1 项 CrewAI）。受信 TLS / 真实 LLM / 真实租户 / 7 天 shadow 仍为 **BLOCKED-需外部**；本文所述"生产栈 bring-up 全容器健康"为 **T7 观测时点**，非当前运行态证明。

---

## 0. 结论速览（Go/No-Go 判断）

| # | 项 | 结论 |
|---|----|------|
| 1 | 发布基线 commit/tag | ✅ **忠实基线已确立**：当前锚点=**`release/v1.0.0-rc4-candidate`**（自 rc3 之上叠加 rc4-candidate 收口）；历史锚点 rc3→`3ccab5c`、rc1→`cd743d3`、rc2→`fa7c9a3`；运行面基线=`cde30fb`（IMAGE_DIGESTS `release_baseline.commit`）+ 迁移修复 696444a |
| 2 | 生产镜像 digest | ✅ 已构建并记录 api / frontend 的 image id + repo digest（worker/migrate 共用 api 镜像；api=5d39f03… 含迁移修复） |
| 3 | 独立生产栈部署 | ✅ **生产栈于 T7 完成一次 bring-up 观测**：compose 迁移 exit 0 + api/worker/frontend/nginx/postgres/redis **当时**全 healthy（api `/api/healthz`=200）；**非当前运行态证明（未经本次复核）** |
| 4 | TLS（受信证书） | 🚫 **BLOCKED-需外部**：现有 certs 为**自签**（CN=preview.local，subject==issuer），非受信 CA |
| 5 | 独立性验证（红线） | ✅ **独立=true**：库名/密码指纹/数据卷/备份目录/子网/项目名/宿主端口 全部与 preview 不同，且 prod 不触碰 `./data/preview-*` |
| 6 | 健康与门控 | ✅ **门控确认**：ENV=production、AUTH_BACKEND=real、BUSINESS_DATA_BACKEND=postgres、EXECUTION_MODE=shadow、LAUNCH_GATE_STRICT=true；compose config VALID；全容器 healthy（**T7 bring-up 观测时点**，非当前运行态证明） |

> **Go/No-Go**：**部署面前置已达成**——发布基线已忠实（**当前锚点=`release/v1.0.0-rc3`→`3ccab5c`==HEAD**；运行面基线=`cde30fb`，接受运行面已提交 8ddca48a）、迁移修复（`shipping_events` 复合外键，696444a）、`BUSINESS_DATA_BACKEND=postgres`（cd743d3）、全新库 compose 迁移 exit 0、生产栈于 T7 bring-up 观测全容器健康（api `/api/healthz`=200，**观测时点，非当前运行态证明**）。**仍不应 Go**（放量）——受信 CA 证书、真实 LLM 评测/端点、首批书面确认租户 **未就绪**（BLOCKED-需外部，见 §7 #4/#5/#6）；EXECUTION_MODE 仍为 shadow（已符合"先 shadow 再 live"门控），真实 LLM 链路也尚未评测。**注**：本文为 T1 期现场记录，当前（rc3）发布锚点=`release/v1.0.0-rc3`→`3ccab5c`；rc1/rc2 为不可移动历史锚点。

---

## 1. 发布基线

### 1.1 Git 状态（实测）

- **当时**（T1，2026-09-04）分支 `main`，`HEAD = 3268a1ca128fedf814835095a6dfb5ec36645b55`（`test(deploy): 更新 clean-context 构建断言以匹配 git archive 上下文`）——此为**历史快照**；当前 HEAD=`3ccab5c`（==`release/v1.0.0-rc3^{commit}`）。
- HEAD tree = `ee3c219118ecff8dd301a7d05ccb601bb9f74524`。
- 既有 tag：`baseline-prod-1..4`：
  - `baseline-prod-1` → `7941246`（**已测功能基线**，见 `evidence/DEPLOYMENT_SUMMARY-20260904-030903.md`、`deploy/records/PRODUCTION_ACCEPTANCE_VERIFICATION.md`）
  - `baseline-prod-2` → `ad168ef`（生产发布基线改造）
  - `baseline-prod-3` → `8be2d97`（钉基础镜像 digest + 锁定依赖基线）
  - `baseline-prod-4` → `03819a8`（`git archive` 固定 mtime 物化构建上下文）
- **工作树大量未提交 + 未跟踪改动**（`git status`）：`src/`、`deploy/`、`docker-compose*.yml` 等 modified；大量未跟踪测试/脚本/evidence。

### 1.2 基线裁定

问题：**"接受并验收的运行面"并非任何单个干净提交**。关键事实：
- 验收证据（PRODUCTION_ACCEPTANCE_VERIFICATION §六）引用的运行面依赖**未跟踪文件**：`src/execution/sandbox_gateway.py`、`src/execution/sandbox_faults.py`、`src/llm/circuit_breaker.py`、`src/tools/data_source.py`、`src/tools/postgres_data_source.py` —— 这些在 HEAD 中**不存在**。
- `src/infrastructure/migrations.py` 在工作树中**新增**了 `shipping_events` 表（HEAD 无该表），且该表存在复合外键缺陷（见 §5.1）。工作树还包含大范围未提交 `src/` 修改（routes/engine/store/postgres_store 等）。
- 因此 **当时 HEAD（3268a1c）不是"已接受系统"的忠实快照**；而 `7941246` 是此前实测的基线，但其后又叠加了生产发布基线改造与可复现构建改动，且同样不含上述未跟踪验收文件。

**裁定**：
- **当时**在 **HEAD=3268a1c**（T1 时点快照）上创建生产候选 tag **`release/v1.0.0-rc1`**（`git tag -a release/v1.0.0-rc1`），作为"可复现构建点"。**注**：rc1 已被后续 rc2/rc3 取代，当前锚点=**`release/v1.0.0-rc3`**→`3ccab5c`；rc1 为不可移动历史锚点。
- **t2 硬化修复（已纳入基线）**：security-auditor 在 t2 发现 `shipping_events` 复合外键缺陷后，修复已应用到工作树并**commit 为 `696444a`**（`fix(migrations): shipping_events 复合外键修复`），`release/v1.0.0-rc1` 已重定向到 `696444a`（tree `c64aed17`；**历史 rc1 动作，当前基线链含 696444a**）。修复后的**全新 prod-like 库 clean migrate 通过**（见 `MIGRATE_VERIFY.md`，exit 0、三表建立、复合 FK 正确、RLS FORCE + 租户 policy 生效、无 InvalidForeignKey）。
- **仍明确声明（当时）**：该 tag（当时 rc1）**尚未**构成**完全**忠实的"已接受系统"基线——接受的运行面**仍有其他未跟踪文件**（`src/execution/sandbox_gateway.py`、`sandbox_faults.py`、`src/llm/circuit_breaker.py`、`src/tools/data_source.py`、`src/tools/postgres_data_source.py` 等）与大量未提交 `src/` 修改**未纳入**任何 commit。要使 release 成为完全忠实基线，需继续 commit 这些接受运行面改动，并经"干净 tag 冷构建 + 全新库迁移"复验。
- 证据：`evidence/prod-go-live/deploy-engineer/git-baseline.txt`（git status / log / tag 转储）、`IMAGE_DIGESTS.json` 的 `release_baseline` 字段。

**为何（当时）选 HEAD 而非 7941246**：7941246 是最先实测的基线，但后续 `ad168ef→3268a1c` 专门为"生产发布基线 + 可复现构建"而做（Dockerfile 钉基础镜像 digest、`git archive` mtime 固定构建上下文、`.env.example` 生产注入清单），是上线所需的构建可复现底座；且 `ad168ef` 加入了大批生产部署记录/观测/演练基础设施。因此以最新可复现构建点 HEAD 作为候选基线，同时诚实标注"接受运行面未入 commit"这一硬缺口。

---

## 2. 镜像 digest

构建命令（docker build，从仓库根 / frontend 目录）。**注意**：本次为验证用，从**脏工作树**构建（反映当时接受码）；**非字节级可复现**——要可复现需先 commit 全部接受运行面改动 + 以 `git archive`（clean-context）在 **`release/v1.0.0-rc3`**（=3ccab5c）上重建（**rc3 未重建**，digest 观测值为 rc1/rc2 运行面基线 `cde30fb` 工作树构建沿用）。

| 服务 | 镜像 tag | image id | repo digest | 构建上下文 / Dockerfile | 基础镜像（digest 钉定） |
|------|----------|----------|-------------|------------------------|------------------------|
| api / worker / migrate | `after-sales-prod-api:latest` | `sha256:5d39f030697e1b00ef83fb22a3358ddd2f923be6752a082d9f31d20f4039f293` | `after-sales-prod-api@sha256:5d39f030697e1b00ef83fb22a3358ddd2f923be6752a082d9f31d20f4039f293` | 仓库根 `.`，`Dockerfile` | `docker.1ms.run/library/python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea` |
| frontend | `after-sales-prod-frontend:latest` | `sha256:12c35ff738c4b29fba0562c7677d086ab15516cbcaf6a2b53f77dc22b2886cad` | `after-sales-prod-frontend@sha256:12c35ff738c4b29fba0562c7677d086ab15516cbcaf6a2b53f77dc22b2886cad` | `./frontend`，`frontend/Dockerfile` | `docker.1ms.run/library/node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293` |

- **出口码说明**：`docker build` 命令 exit 1 仅为 BuildKit 进度输出到 stderr 被 PowerShell 视为错误流；实际镜像已成功命名并 unpack（见 `IMAGE_DIGESTS.json`）。可用 `docker images --digests` 复核。
- 依据 `docker-compose.prod.yml`，api/worker/migrate 三服务共用 `after-sales-prod-api` 镜像（`src.main`/celery/`migrate_cli` 由 CMD/command 区分），故仅需一个后端镜像 + 一个前端镜像。
- 证据：`evidence/prod-go-live/deploy-engineer/IMAGE_DIGESTS.json`。

---

## 3. 独立生产栈部署（配置）

与 preview 完全隔离的独立项目 `after-sales-prod`。交付物（脚本/配置/模板）：

| 文件 | 说明 |
|------|------|
| `deploy/.env.production` | **真实密钥注入文件（已 gitignore，不提交）**：独立库名 + all-random 独立密钥（见 §3.1） |
| `deploy/.env.production.example` | 可提交的生产注入模板（占位符，**不含真实密钥**） |
| `docker-compose.prod.yml` | 独立生产组合：`name: after-sales-prod`、独立子网 `172.31.0.0/16`、独立卷 `./data/prod-*`、独立宿主端口 `8080/8843`、独立项目/网络名 |
| `deploy/nginx/prod.conf` | 生产反向代理 + HTTPS（同源，`listen 443 ssl`；`server_name` 为真实生产域名占位） |
| `.gitignore` | 已追加 `deploy/.env.production`（防真实密钥入仓库）；`data/` 本已忽略 |

### 3.1 `.env.production` 独立密钥清单（每项独立随机生成，不复用 preview）

| 变量 | 值来源 | 与 preview 关系 |
|------|--------|----------------|
| `POSTGRES_DB` | `after_sales_prod` | **独立库名**（preview=`langgraph`） |
| `POSTGRES_PASSWORD` | 随机 hex 32 字节 | 独立随机，指纹≠preview |
| `APP_RUNTIME_PASSWORD` | 随机 hex 32 字节 | 独立随机 |
| `BACKUP_ROLE_PASSWORD` | 随机 hex 32 字节 | 独立随机 |
| `BACKUP_ENC_KEY` | `openssl rand -base64 32` | 独立随机 |
| `REDIS_PASSWORD` | 随机 hex 32 字节 | 独立随机 |
| `AUTH_JWT_SECRET` | 随机 hex 48 字节 | 独立随机 |
| `EXECUTION_CALLBACK_HMAC_SECRET` | 随机 hex 32 字节 | 独立随机（≠默认 `shadow-callback-secret`） |
| `LLM_API_KEY` | 随机 hex 24 字节占位 | 真实自托管端点确认前 mock |
| `AUTH_LOGIN_CREDENTIALS` | `{}`（fail-closed） | 真实租户哈希须由 release-manager 注入 |
| `LAUNCH_ALLOWED_TENANTS` | `__NONE_APPROVED_YET__` | 书面确认首批租户后注入（当前 fail-closed） |
| `HIGH_CONFIDENCE_MODELS` | `__NO_VERIFIED_WRITE_MODEL__` | 无真实评测 → 交集为空 → 写操作全部 fail-closed 转人工 |

> 说明：业务输入（真实租户/真实 LLM 评测/真实网关）未提供 → 采用 **fail-closed 默认**（空/占位），绝不虚设模型或假造"已放行租户"。

---

## 4. TLS（受信证书）

### 4.1 现状（实测）
- 现有证书：`deploy/secrets/certs/server.crt` / `server.key`。
- 实测（.NET X509Certificate2）：
  - Subject = `CN=preview.local`
  - Issuer = `CN=preview.local`（**subject == issuer → 自签**）
  - NotBefore = `2026-09-04`，NotAfter = `2027-09-04`（1 年）
  - Thumbprint = `2CFDF1A3625F59882C76E8902AAD81D95BE409D0`

### 4.2 处置方案（受信，待外部输入）
- **硬前提**：需真实受信 CA 签发证书（域名 + 证书链 + 私钥）。本机**没有**真实受信 CA 证书，仅有上述自签证书。
- 方案：
  1. 确认正式生产**域名**（如 `admin.after-sales.<tld>`），由受信 CA（公共 CA 或企业 CA）签发票据链。
  2. 将 `fullchain.pem`（含中间链）+ `privkey.pem` 替换 `deploy/secrets/certs/server.crt/server.key`（或按 nginx 挂载路径）。
  3. `deploy/nginx/prod.conf` 已就绪：`ssl_certificate /etc/nginx/certs/server.crt`、`ssl_certificate_key ...`，TLSv1.2/1.3，HSTS。仅需把 `server_name` 改为真实域名、替换证书链。
  4. 证书链正确性：`openssl s_client -connect host:8843` 复核链完整（本机无 openssl CLI，可用容器/`deploy/scripts` 复验）。
- **状态：🚫 BLOCKED-需外部**（需：真实域名 + 受信 CA 证书链）。在替换前，生产 nginx 虽可起（用自签）但**浏览器/客户端不信任**，不得视为"已受信"。

---

## 5. 独立性验证（关键红线）

### 5.1 【已修复，t2 硬化项】迁移 → 独立
> **更新（t2 修复）**：本缺陷已修复并复验——`src/infrastructure/migrations.py` 的 `shipping_events` 已改为表级复合外键 `FOREIGN KEY (tenant_id, order_id) REFERENCES orders(tenant_id, order_id) ON DELETE CASCADE`（提交 `696444a`），并在**全新 prod-like 库**复跑 `migrate_cli` 通过（exit 0、三表建立、RLS FORCE 生效、无 InvalidForeignKey，见 `MIGRATE_VERIFY.md`）。以下为**修复前**的失败记录（存档）。

首测（修复前）尝试 `docker compose --env-file deploy/.env.production -f docker-compose.prod.yml up -d`。
- postgres/redis 健康；**但 `migrate` 退出 1**，阻塞后续 api/worker/frontend（依赖 `migrate` 成功）。
- `docker logs after-sales-prod-migrate-1`：
  ```
  sqlalchemy.exc.ProgrammingError: (psycopg.errors.InvalidForeignKey)
  number of referencing and referenced columns for foreign key disagree
  [SQL: CREATE TABLE IF NOT EXISTS shipping_events (
      tenant_id TEXT NOT NULL REFERENCES tenants(id),
      order_id TEXT NOT NULL REFERENCES orders(tenant_id, order_id) ON DELETE CASCADE,
      tracking_no TEXT, events JSONB NOT NULL DEFAULT '[]', PRIMARY KEY (tenant_id, order_id) )]
  ```
- **根因**：`src/infrastructure/migrations.py` 的 `shipping_events` 表把 `order_id` 单列引用**复合键** `orders(tenant_id, order_id)`（1 引用列 vs 2 被引用列）。该 `shipping_events` 表是**未提交新增**（`git diff HEAD` 显示新增 `shipping_events`；HEAD 无该表）。**接受的运行面无法在全新库上完成迁移**。
- 处理：为避免留下孤立资源，已 `docker compose ... down` 该隔离栈；**preview 栈未受影响**（7 容器健康，postgres running）。产物：`evidence/prod-go-live/deploy-engineer/MIGRATE_FAILURE.log`（日志摘录）。

### 5.2 独立性对比（红线 —— prod 与 preview 互不复用）

证据 `evidence/prod-go-live/deploy-engineer/INDEPENDENCE.json`，实测结论：

| 维度 | preview（after-sales-preview） | prod（after-sales-prod） | 独立? |
|------|------|------|------|
| 项目名 | `after-sales-preview` | `after-sales-prod` | ✅ |
| PG 库名 | `langgraph` | `after_sales_prod` | ✅ |
| PG 密码指纹 (sha256) | `...` | `...` | ✅ 不同 |
| 数据卷 | `./data/preview-postgres`、`./data/preview-backups` | `./data/prod-postgres`、`./data/prod-backups`、`./data/prod-backups-offsite` | ✅ 不相交 |
| 备份目录 | `./data/preview-backups` | `./data/prod-backups` | ✅ |
| 网络子网 | `172.30.0.0/16` | `172.31.0.0/16` | ✅ |
| 宿主端口 | `80:80`、`443:443` | `8080:80`、`8843:443` | ✅ 不相交 |
| nginx 配置 | `deploy/nginx/preview.conf` | `deploy/nginx/prod.conf` | ✅ |
| 密钥来源 | `deploy/.env.preview` | `deploy/.env.production`（独立随机） | ✅ |
| prod 触碰 `./data/preview-*` | — | 无（卷/路径均 `prod-*`） | ✅ |

**`independent = true`**。生产栈不触碰 preview 的任何容器/卷/备份/密钥/网络/端口。

---

## 6. 健康与门控

### 6.1 门控配置（确认）
- `src/config.py::is_restricted_env()`：`return self.env in {"preview","production"}` → ENV=production 为受限环境。
- `src/main.py::fail_closed_auth_guard()`：受限环境 + `AUTH_BACKEND != "real"` → `raise RuntimeError`（**Mock 认证启动即失败**）；受限环境 + real 但缺 `AUTH_JWT_SECRET` → `raise RuntimeError`。代码证据见 `src/main.py:91-110`。
- `docker-compose.prod.yml`：`ENV=production`、`AUTH_BACKEND=real`、`STORAGE_BACKEND=postgres`、`EXECUTION_MODE=shadow`、`EXECUTION_PROVIDER=mock`、`LAUNCH_GATE_STRICT=true`、`DEMO_SEED_ENABLED=false`。
- `docker compose config`（`--env-file deploy/.env.production -f docker-compose.prod.yml`）：**VALID**（`name: after-sales-prod`，服务正确插值）。

### 6.2 生产栈健康 **已于 T7 完成一次 bring-up 观测（当时全容器 healthy）——非当前运行态证明**
- **迁移 + 全栈健康（t2 硬化 + BUSINESS_DATA_BACKEND 修复后，compose 实测；T7 bring-up 观测时点，非当前运行态证明）**：`docker compose --env-file deploy/.env.production -f docker-compose.prod.yml up -d` 后，`migrate` exit 0（`migrations applied; runtime role and backup role ensured.`），api/worker/frontend/nginx/postgres/redis 全部 **healthy**，`after-sales-prod-nginx-1` 暴露宿主 **8080/8843**。健康端点实测（`PROD_STACK_HEALTH.md`）：nginx `/healthz`(8080)=`200`；api `/api/healthz`(8843)=`200` body=`{"status":"ok"}`；frontend `/`(8843)=`200`；`/api/metrics`(公网)=`404`（内网化正确阻断）。
- **api 启动另需关键配置**：受限环境（production）launch gate 强制 `BUSINESS_DATA_BACKEND=postgres`（mock 仅限测试），否则 api 启动即 fail-closed。已在 `docker-compose.prod.yml`（api/worker）与 `deploy/.env.production.example` 加入（commit `cd743d3`；生产栈 healthz 200 为 **T7 bring-up 观测时点**结果）。
- **库/RLS/角色**（生产 `after_sales_prod`，owner=migrator）：`orders/shipping_events/policy_documents` 存在；`shipping_events` 复合外键 `shipping_events_tenant_id_order_id_fkey` 正确；三表均 `enabled=true forced=true policies=1`；`app_runtime`/`backup_role`（inherit=false super=false createdb=false createrole=false canlogin=true）最小权限角色。
- **not 构成 blocker**：`shipping_events` 复合外键（t2）与 `BUSINESS_DATA_BACKEND` 均不阻塞。
- **仍未实测/属外部输入**（如实标注）：受信 TLS（nginx 443 现用自签，**BLOCKED-需外部**）；真实 LLM 评测/端点（LLM_BACKEND=mock，**BLOCKED-需外部**）；首批书面确认真实租户。以及生产栈 RLS 动态复验 / 告警端到端 / 应用侧 Langfuse trace——放量前部署期执行项。

---

## 7. 前置未达成清单（放量门控项）

| # | 前置 | 状态 | 所需输入 |
|---|------|------|---------|
| 1 | 接受运行面（sandbox_gateway/sandbox_faults/circuit_breaker/data_source/postgres_data_source 等 + 部署配置）commit 形成完整忠实基线 | ✅ **已提交**（commit 8ddca48a，140 文件；+cd743d3 补 BUSINESS_DATA_BACKEND） | 已做：基线链至 cd743d3/`cde30fb`；当前锚点=`release/v1.0.0-rc3`=`3ccab5c` |
| 2 | 生产镜像基于**忠实基线 + 干净上下文**重建 | ⚠️ **待干净重做**（当前为工作树构建，代码与基线一致；需 git archive clean-context 使字节级可复现） | 见 #1；`git archive` + `docker build --no-cache` 复现 |
| 3 | 全新库迁移（`shipping_events` 复合外键缺陷） | ✅ **已修复复验**（commit 696444a） | 已做：compose migrate exit 0、三表建立、RLS 生效（MIGRATE_VERIFY.md） |
| 4 | 受信 CA 证书链 + 生产域名 | 🚫 BLOCKED-需外部 | 真实域名 + 受信 CA 证书（fullchain+key） |
| 5 | 真实自托管 LLM 端点 + API key + 网络白名单 + 写操作评测 | 🚫 BLOCKED-需外部 | 真实 LLM 端点/密钥 + `llm_candidate_eval.json`(write_op_pass=true) |
| 6 | 首批**书面确认**真实租户 + argon2id 登录凭据 | 🚫 BLOCKED-需外部 | release-manager：首批租户书面确认记录 + `hash_login_credentials.py` 产物 |
| 7 | 生产栈容器级健康（api/healthz→nginx→前端） | ✅ **已于 T7 一次 bring-up 观测验证**（当时全容器 healthy，api/healthz=200；**非当前运行态证明**） | 已做：PROD_STACK_HEALTH.md |
| 8 | 告警/RLS/灾备容器实跑复验（部署期执行项） | ⚠️ 未实测 | `docker compose up` 后执行 `deploy/drills/verify_dr_compose.sh` 等 |

> **诚实边界**：本机**无**真实受信 CA 证书、真实域名、真实 LLM 端点与密钥、书面确认的真实租户。故对应项均如实标注 BLOCKED-需外部，绝不伪造"已受信/已放行/已评测/已恢复"。

---

## 8. 证据文件清单

| 证据 | 路径 |
|------|------|
| 发布基线（git 转储） | `evidence/prod-go-live/deploy-engineer/git-baseline.txt` |
| 镜像 digest | `evidence/prod-go-live/deploy-engineer/IMAGE_DIGESTS.json` |
| 独立性验证 | `evidence/prod-go-live/deploy-engineer/INDEPENDENCE.json` |
| migrate 失败日志（修复前存档） | `evidence/prod-go-live/deploy-engineer/MIGRATE_FAILURE.log` |
| 【t2 硬化】全新库 clean migrate 复验 | `evidence/prod-go-live/deploy-engineer/MIGRATE_VERIFY.md` |
| 【T7】生产栈全容器健康 + 健康端点 | `evidence/prod-go-live/deploy-engineer/PROD_STACK_HEALTH.md` |
| 【T7】忠实基线 commit 范围 | `evidence/prod-go-live/deploy-engineer/BASELINE_COMMIT_SCOPE.md` |
| 生产注入模板 | `deploy/.env.production.example` |
| 生产 compose | `docker-compose.prod.yml` |
| 生产 nginx | `deploy/nginx/prod.conf` |
| 真实密钥（未入库） | `deploy/.env.production`（已 gitignore） |

*deploy-engineer · T1 交付（含 t2 硬化修复项 `shipping_events` 复合外键 + BUSINESS_DATA_BACKEND，全新库 clean migrate + 生产栈 T7 bring-up 全容器健康观测）。本文为 T1 现场记录；当前发布锚点=`release/v1.0.0-rc3`=`3ccab5c`。*
