# 生产部署记录（模板骨架）

> **填表说明**：本文件是**模板/骨架**，字段均为 `<...>` 占位，由 **t1（部署基线）** 实跑后回填，或由
> t2/t3 交叉验证后补录。切勿在此处臆造数值；只有**本次真实观测到的事实**才可填入「当前可观测事实」小节。
> 最终文件名约定：`deploy/records/PRODUCTION_DEPLOYMENT_record-<ts>.md`（`<ts>` 为部署时间戳，如
> `PRODUCTION_DEPLOYMENT_record-202609041030.md`）。

---

## 0. 记录标识
- 记录 ID：`PROD_DEPLOY-<ts>`
- 部署目标环境：`<preview / production>`（本阶段为 `preview`，项目名 `after-sales-preview`）
- 部署时间戳 `<ts>`：______（填：`YYYYMMDD-HHMM`）
- 执行人 / 复核人：`<t1 执行人> / <复核人>`
- 数据来源：`deploy/scripts/deploy.sh` 实跑输出 + `compose ps` + `healthcheck.sh` 结果（t1）

---

## 1. 拓扑与暴露面
| 项 | 值 | 回填来源 |
|---|----|---------|
| Compose 项目名 | `after-sales-preview` | t1 |
| 暴露面（edge 网络） | 仅 `nginx` 发布 **80/443** | t1 |
| 私网（internal 网络） | `postgres` `redis` `api` `worker` `frontend` `migrate`（不发布宿主端口） | t1 |
| nginx 反向代理规则 | `/api/* → api:8000`；其余 → `frontend:3000`；80→301 443 | t1 |
| 观测栈（自托管） | `after-sales-observability`（prometheus/grafana/loki/langfuse/promtail，`obs` 内网 `internal:true`） | t1/t2 |

## 2. 组件版本
| 组件 | 镜像/版本 | 回填来源 |
|------|-----------|---------|
| PostgreSQL | `docker.1ms.run/library/postgres:17-alpine`（DB=`langgraph`，owner=`migrator`） | t1 |
| Redis | `docker.1ms.run/library/redis:7-alpine`（requirepass） | t1 |
| API | `Dockerfile`（`python -m src.*`，`api:8000`） | t1 |
| Worker | `Dockerfile`（`celery -A src.tasks.worker worker`） | t1 |
| Frontend | `./frontend/Dockerfile`（Next.js 生产） | t1 |
| nginx | `docker.1ms.run/library/nginx:1.27-alpine` | t1 |
| migrate | `Dockerfile`（`python -m src.infrastructure.migrate_cli`，一次性） | t1 |
| 运行角色 | `app_runtime`（LOGIN NOINHERIT NOSUPERUSER NOBYPASSRLS，仅 DML） | t1/t2 |

## 3. Git 引用与构建
- 部署 git 引用：`<t1 记录 deploy.sh 输出的 ref>`（`deploy/scripts/deploy.sh [git-ref]`，缺省=当前 HEAD）
- **git commit（完整 / 短）**：`<git rev-parse <ref>^{commit} 的完整哈希>` / `<前 7 位>`（`deploy.sh` 第 0/8 步解析；如 `7941246a0ec95f2ecd404138beb87c59a8d7caba` / `7941246`）
- 当前仓库可观测 HEAD：`7941246`（branch `main`，`git rev-parse --short HEAD`）——**此为当前可观测事实，非本轮部署实际引用，由 t1 最终确认**
- 构建方式：**干净 worktree**（`deploy.sh` 第 6/8 步 `common.sh::build_from_worktree`：`git worktree add <tmp> <ref>` 从该引用检出干净 tree，build context 仅含该引用下已提交源码）→ `compose build --pull`
- 工作区干净度：**部署前必须 `git status --porcelain` 为空**（`deploy.sh` 第 0/8 步 fail-closed；除非显式 `--allow-dirty`）。**绝不从脏工作区构建。**
- 构建时间：`<date '+%Y-%m-%d %H:%M:%S %z'>`（`BUILD_TIMESTAMP`，`deploy.sh` 第 6/8 步）
- **环境配置版本（DEPLOY_CONFIG_VERSION）**：`<src/config.py Settings().deploy_config_version 或 ENV_FILE DEPLOY_CONFIG_VERSION>`（发布记录必填，配置漂移审计）
- **镜像 digest（repo digest / image id）**：`<镜像 content Id（sha256:...）+ RepoDigest，如 after-sales-preview-api@sha256:...>`（`build_from_worktree` 用 `docker image inspect` 采集，写入 `BUILD_DIGESTS`）
- 构建镜像 tag / 时间：`<t1>`
- `deploy/backups/rollback.log` 追加行：`deploy ref=<ref> commit=<full> config_version=<cfg> build_ts=<ts> digests=<svc=digest,...>`

### 可复现性检查（同 ref 干净重建）
- 同 ref（tag 或 commit）从**另一干净 worktree** 重建，是否得到同一镜像 digest：`<PASS/FAIL/待实测>`
- 备注：若 `requirements.txt` 含未精确钉住的 `>=` 版本（如 `fastapi>=`、`langchain-core>=`），长期重建**可能**解析到更新依赖导致 digest 漂移；可复现性依赖依赖锁定与 base 镜像稳定。**验收建议用同一干净 tag 连续构建两次比对。**

## 4. 环境与关键配置（门控变量）
| 变量 | 值 | 回填来源 |
|------|----|---------|
| `ENV` | `preview` | t1 |
| `STORAGE_BACKEND` | `postgres` | t1 |
| `AUTH_BACKEND` | `real` | t1/t2 |
| `AUTH_CREDENTIAL_HASH` | `argon2id`（默认）\|`bcrypt`（禁 sha256/md5） | t2/t5 |
| `DEMO_SEED_ENABLED` | `false`（preview/production 禁用 seed_default；真实租户走受控迁移/运维脚本） | t2/t5 |
| `LOGIN_RATE_LIMIT_STORE` / `LOGIN_RATE_LIMIT_WINDOW_SECONDS` / `LOGIN_ACCOUNT_RATE_LIMIT` / `LOGIN_IP_RATE_LIMIT` | `memory` / `300` / `5` / `30` | t2/t5 |
| `LOGIN_BACKOFF_MAX_FAILURES` / `LOGIN_BACKOFF_BASE_SECONDS` / `LOGIN_BACKOFF_MAX_SECONDS` / `LOGIN_TRUSTED_PROXY_DEPTH` | `5` / `1.0` / `900` / `1` | t2/t5 |
| `DEPLOY_CONFIG_VERSION` | `0.1.0`（发布记录配置版本） | t4/t5 |
| `ENABLE_CORS` | `false` | t1/t2 |
| `LAUNCH_ALLOWED_TENANTS` | `<首批租户，如 TENANT-A,TENANT-B>` | t1 |
| `LAUNCH_GATE_STRICT` / `LAUNCH_REQUIRE_APPROVAL` / `LAUNCH_FULL_AUDIT` / `LAUNCH_MANUAL_REVIEW` | `true` | t1/t2 |
| `EXECUTION_MODE` / `EXECUTION_PROVIDER` | `shadow` / `mock`（沙箱，不上真实资金） | t1/t2 |
| `LLM_BACKEND` / `LLM_BASE_URL` / `LLM_MODEL` | `openai_compatible` / `<内网端点>` / `<model>` | t1 |
| `LLM_ALLOWED_HOSTS` | `<端点网络白名单>` | t1/t2 |
| `HIGH_CONFIDENCE_MODELS` | `<评测 write_op_pass=true 的 model id>` | t1/t2 |
| `LLM_EVAL_REPORT_PATH` | `/app/evidence/llm_candidate_eval.json`（只读挂载） | t1/t2 |
| `EXECUTION_CALLBACK_HMAC_SECRET` | `<注入，禁止默认值>` | t1/t2 |
| `LOG_JSON_FORMAT` / `LOG_LEVEL` | `true` / `INFO` | t1/t2 |

## 5. 健康检查结果（`bash deploy/scripts/healthcheck.sh`）
| # | 核查项 | 结果 | 备注 |
|---|--------|------|------|
| [1] | 宿主仅开放 80/443（`ss`/`netstat`；无 5432/6379/8000/3000） | `<PASS/FAIL>` | 监听端口：`<list>` |
| [2] | HTTPS 同源：`https://host/` 与 `https://host/api/openapi.json` 均 200；无 `Access-Control-Allow-Origin` | `<PASS/FAIL>` | 实际 HTTP code：`<...>` |
| [3] | 数据库/Redis 无公网监听：宿主无 5432/6379；`compose port` 无业务端口发布 | `<PASS/FAIL>` | `compose port` 输出：`<...>` |
| [4] | Mock / 缺 JWT 密钥 fail-closed（`verify_fail_closed.sh`） | `<PASS/FAIL>` | `EXIT=<0/non-zero>` |
- 健康检查总结：`<X 项通过 / Y 项失败>`；是否退出 0：`<是/否>`

## 6. 仅 80/443 验证（反向代理暴露面）
- 80 是否强制 HTTPS（301）：`<是/否>`（`deploy/nginx/preview.conf` server 块 listen 80 → return 301）
- 443 是否 HTTPS 终结：`<是/否>`（`listen 443 ssl`，SSL 协议 `TLSv1.2 TLSv1.3`）
- 是否检测到任何内网业务端口对外暴露：`<无/有>`（出现即违规）

## 7. TLS 证书
- 证书路径：`deploy/secrets/certs/server.crt` / `server.key`（已 gitignore，`gen_certs.sh` 生成）
- 证书 CN / SAN：`CN=<preview.local>`，`SAN=DNS:preview.local,DNS:localhost,IP:127.0.0.1`（预览自签名，默认）
- 证书有效期 / 签发者：`<openssl x509 -in ... -noout -subject -issuer -dates>` —— **生产需替换为受信 CA**（t1/t2 注明）
- 生产受信 CA 证书链：`<未配置 / 受信 CA 引用>`（由 t1/t2 核验）—— **当前为自签名，仅内部预发布，不作为生产受信凭证**

## 8. 上线门控三道闸门结果（`deploy.sh` 依次执行）
| 闸门 | 命令 | 结果 | 备注 |
|------|------|------|------|
| 1 密钥检查 | `check_secrets.sh` | `<PASS/FAIL>` | 缺机密/占位/内建默认回调密钥/`LAUNCH_GATE_STRICT!=true` 即拒 |
| 2 严格上线门控 | `verify_launch_gate.py --strict` | `<PASS/FAIL>` | 首批少量租户/shadow/仅审批后/全量审计/人工复核/PG 数据面 |
| 3 Compose 配置 | `compose config --quiet` | `<PASS/FAIL>` | `${VAR:?}` 必需变量与拓扑合法性 |
| 4 fail-closed 验证 | `verify_fail_closed.sh` | `<PASS/FAIL>` | 受限环境 Mock/缺密钥启动即失败 |

## 9. 回滚就绪
- 部署引用写入 `deploy/backups/rollback.log`：`deploy ref=<ref> ts=<ts>`
- 变更前备份：`<备份 dump 路径>`（`deploy/scripts/backup_db.sh`）
- 回滚入口：`bash deploy/scripts/rollback.sh <backup-file> <git-ref>`（见回滚记录模板）

## 10. 当前可观测事实（非本轮实测部署值，仅存疑记录）
- 仓库当前 HEAD：`7941246`（`git rev-parse --short HEAD`，branch `main`）
- 说明：本会话此前阶段（`pg-acceptance-continue` 等）已有容器级/数据面证据（见 `evidence/*.json`），
  **t1 需在实机完成 `deploy.sh` + `healthcheck.sh` 后刷新本记录为真实值**，否则本记录仅为骨架，不为已达标凭证。

## 结论（待 t1 回填，勿在此臆造）
- 部署是否成功：`<是/否>`（`compose ps` 全部 running；`healthcheck.sh` 退出 0）
- 是否达到 preview 验收：`<达到/未达到>`（未实机验证不得填写"达到"）
- 签名：______
