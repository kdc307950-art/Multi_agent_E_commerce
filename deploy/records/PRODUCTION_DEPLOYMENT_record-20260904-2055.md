# 生产部署记录（正式交付 · after-sales-prod）

> 本记录面向**独立生产栈 `after-sales-prod`**（与 preview 栈 `after-sales-preview` 完全隔离）。
> 值来自 **t6（启动独立生产栈）/ t7（全新库迁移，队长接管）/ t8（全栈健康验证）** 真实产物；
> 未实测项一律如实标注，不声称达标。时间戳：`20260904-2055`（复盘，栈实际拉起约 2026-09-04 14:30）。

---

## 0. 记录标识
- 记录 ID：`PROD_DEPLOY-20260904-2055`
- 部署目标环境：**`production`**（compose 项目名 `after-sales-prod`）
- 部署时间戳：`2026-09-04 14:30`（首启）→ `2026-09-04 20:5x`（全新迁移复验）
- 执行人 / 复核人：`deploy-engineer`(t6/t7) / `security-auditor`(t8)（本轮由 AgentTeams 协作）
- 数据来源：`docker-compose.prod.yml` + `deploy/.env.production` + `compose run migrate` + `compose ps` + 容器级 readiness 探测

## 1. 拓扑与暴露面
| 项 | 值 | 依据 |
|---|----|------|
| Compose 项目名 | `after-sales-prod`（compose `name:`） | t6 |
| 暴露面（edge 网络） | 仅 `nginx` 发布宿主 **8080→80 / 8843→443**；其余容器 `PortBindings={}` 无宿主端口发布 | t6/compose config |
| 私网（internal） | `postgres` `redis` `migrate` `api` `worker` `frontend` `backup`（不发布宿主端口） | t6 |
| internal 子网 | `172.31.0.0/16`（`networks.internal.ipam`；与 preview `172.30.0.0/16` 隔离） | t5/t6 |
| nginx 代理 | `/api/* → api:8000`；其余 → `frontend:3000`；8080 仅 healthz/重定向 | prod.conf |
| 观测栈 | `after-sales-observability`（prometheus 已接入 `prod-net`=after-sales-prod_internal 抓取链路） | t3 |

## 2. 组件版本
| 组件 | 镜像/版本 | 依据 |
|------|-----------|------|
| PostgreSQL | `docker.1ms.run/library/postgres:17-alpine`（DB=`after_sales_prod`，owner/web `migrator`=Superuser） | t6 |
| Redis | `docker.1ms.run/library/redis:7-alpine`（requirepass） | t6 |
| API | `after-sales-prod-api:latest`（id `sha256:5d39f030…`；Dockerfile→`python -m src.*`，`api:8000`） | t6/t8 |
| Worker | `after-sales-prod-api:latest`（`celery -A src.tasks.worker worker`） | t6 |
| Frontend | `after-sales-prod-frontend:latest`（id `sha256:12c35f…`；Next.js 生产） | t6 |
| nginx | `docker.1ms.run/library/nginx:1.27-alpine` | t6 |
| migrate | `after-sales-prod-api`（`python -m src.infrastructure.migrate_cli`，一次性，exit=0） | t6/t7 |
| **backup** | `after-sales-prod-backup:latest`（id `sha256:9cf30e…`；基于 `postgres:17-alpine` 补装 openssl+rsync，见 deploy/backup.Dockerfile） | 本轮备份修复 |
| 运行角色 | `app_runtime`（LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS，仅 DML） | t7 |
| 备份角色 | `backup_role`（LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE **BYPASSRLS**，仅 SELECT 只读） | t7 |

## 3. Git 引用与构建
- 部署 git 引用（当前仓库可观测 HEAD）：`59e37f2`（短）/ branch `main`
- **环境配置版本（DEPLOY_CONFIG_VERSION）**：`0.1.0`（`src/config.py` 默认；`deploy/.env.production` 未显式覆盖）
- 构建方式：`docker compose -f docker-compose.prod.yml build`（本地构建 `after-sales-prod-api`/`--frontend`/`--backup`）；backup 由 `deploy/backup.Dockerfile` 构建
- 证书：`deploy/secrets/certs/server.crt`/`server.key`（自签，CN=preview.local）—— **受信 CA 为生产硬前提，当前自签 → BLOCKED**（已知项，见 §8）

## 4. 环境与关键配置（`deploy/.env.production`）
| 变量 | 值 |
|------|----|
| `ENV` / `STORAGE_BACKEND` / `BUSINESS_DATA_BACKEND` | `production` / `postgres` / `postgres` |
| `AUTH_BACKEND` | `real` |
| `POSTGRES_DB` | `after_sales_prod` |
| `APP_RUNTIME_USER` | `app_runtime` |
| `BACKUP_ROLE_USER` | `backup_role`（`BACKUP_ROLE_PASSWORD` 密钥注入） |
| `LAUNCH_GATE_STRICT` / `LAUNCH_REQUIRE_APPROVAL` / `LAUNCH_FULL_AUDIT` / `LAUNCH_MANUAL_REVIEW` | `true` |
| `LAUNCH_ALLOWED_TENANTS` | `__NONE_APPROVED_YET__`（不向外部放行 → 门控拒绝所有，fail-closed） |
| `DEMO_SEED_ENABLED` | `false` |
| `EXECUTION_MODE` / `EXECUTION_PROVIDER` | `shadow` / `mock`（沙箱，不上真实资金） |
| `LLM_BACKEND` | `mock`（真实端点未达前） |
| `HIGH_CONFIDENCE_MODELS` | `__NO_VERIFIED_WRITE_MODEL__`（所有写操作转人工，安全默认） |
| `METRICS_ALLOWED_SOURCES` | `172.31.0.0/16`（与内部子网闭环） |
| `BACKUP_INTERVAL_SECONDS` / `BACKUP_KEEP` | `900` / `8`（RPO 上界=备份周期 15min） |
| AUTH_LOGIN_CREDENTIALS | `{}`（空；未放行租户，登录一律拒绝并审计） |
| 独立密钥 | `POSTGRES_PASSWORD`/`APP_RUNTIME_PASSWORD`/`BACKUP_ROLE_PASSWORD`/`BACKUP_ENC_KEY`/`REDIS_PASSWORD`/`AUTH_JWT_SECRET`/`EXECUTION_CALLBACK_HMAC_SECRET`（与 preview 逐字节不同，见隔离报告） |

## 5. 全新库迁移（t7，队长接管执行）
- 安全快照：`deploy/backups/pre_fresh_migrate_20260904204752.dump`（53KB，pg_dump -Fc，`pg_restore --list`=143 项可恢复）
- 迁移动作：`DROP SCHEMA public CASCADE`（级联删除 19 对象）→ `CREATE SCHEMA public` → `compose run --rm --no-deps migrate`（`migrations applied; runtime role and backup role ensured.`，exit=0）
- 验证结果：
  - **17 张业务表**（tenants/memberships/sessions/operations/approvals/executions/streams/stream_events/audit/orders/shipping_events/policy_documents/checkpoint_thread_scopes + 官方 checkpoints/checkpoint_blobs/checkpoint_writes/checkpoint_migrations）
  - **roles**：`app_runtime`(No inheritance, NOBYPASSRLS)、`backup_role`(No inheritance, Bypass RLS)、`migrator`(Superuser, Create role, Create DB, Replication, Bypass RLS)
  - **15 张表 FORCE RLS + 15 条 `*_tenant_scope` policy**（`tenants` 为平台主数据，按设计**不启用** RLS；其余业务表 + 官方 checkpoint 表全启用，policy 用 `app_current_tenant_id()` / `checkpoint_thread_in_current_tenant(thread_id)`）
  - 基线行数：`tenants=0, orders=0`（干净状态）

## 6. 全栈健康（t8 readiness）
| # | 组件 | 命令 | 结果 |
|---|------|------|------|
| 1 | postgres | `pg_isready -U migrator -d after_sales_prod` | `accepting connections` ✅ |
| 2 | redis | `redis-cli -a $REDIS_PASSWORD ping` | `PONG` ✅ |
| 3 | api | 容器内 `/api/healthz` | `200 {"status":"ok"}` ✅ |
| 4 | worker | `celery inspect ping` | `celery@…: OK pong/1 node online` ✅ |
| 5 | frontend | 容器内 `fetch(:3000)` | `200` ✅ |
| 6 | nginx | `http://127.0.0.1:8080/healthz`（及 8843 `/api/healthz`、`/`） | `200 ok` ✅ |
- 端到端：`https://127.0.0.1:8843/api/healthz` → 200；`https://8843/` → 200。

## 7. 备份说明（本轮修复）
- 原缺陷：backup sidecar 用 `postgres:17-alpine`（无 openssl CLI），`openssl enc` 加密失败 → 所有 `.dump.enc` **0 字节 BACKUP_FAIL**。
- 修复：新建 `deploy/backup.Dockerfile`（基于 `postgres:17-alpine` 补装 `openssl`+`rsync`），构建 `after-sales-prod-backup` 镜像，`docker-compose.prod.yml` backup 服务改用该镜像。
- 验证：重建后产出 `langgraph-20260904125115.dump.enc`（**53584 字节**，`BACKUP_OK`，SHA-256 已生成并落盘）。15 分钟自动加密备份链路恢复。

## 8. 已知限制（如实，非本轮达标项）
- **TLS 为自签证书**（CN=preview.local，非受信 CA）：`nginx /healthz` 仅 HTTP:80(8080)；HTTPS:443 无 `/healthz` location（监听本身健康）；`/api/openapi.json` 404。**受信 CA 为生产硬前提，当前自签 → BLOCKED**，未达对外可信标准（发布侧排期）。
- **生产仍未向外部放行租户**：`LAUNCH_ALLOWED_TENANTS=__NONE_APPROVED_YET__`、`AUTH_LOGIN_CREDENTIALS={}`、`HIGH_CONFIDENCE_MODELS=__NO_VERIFIED_WRITE_MODEL__` → 门控 fail-closed，属**设计上的安全默认态**，未批准放量。

## 结论
- 部署是否成功：**是**（t6 起 7 服务全 Up 且 healthy；t8 readiness 全过；`/api/healthz=200`）
- 全新库迁移：**通过**（17 表 + 角色分离 + 15 表 FORCE RLS + 15 policy，干净状态）
- 隔离校验：**通过**（密钥/数据卷/备份目录/网络子网/项目与容器名 与 preview 完全隔离）
- 签名：`deploy-engineer` / `security-auditor`
