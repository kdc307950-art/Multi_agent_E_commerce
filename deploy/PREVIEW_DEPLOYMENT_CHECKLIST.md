# Preview 环境部署 · 迁移 · 回滚 · 健康检查清单

> **定位**：本文档是"以 preview 环境运行"的可重复执行清单，配套已改造的 `docker-compose.preview.yml`
> 与 `deploy/scripts/*`、`src/` 的 fail-closed 与角色分离改动。执行前请通读，任何一项不满足即视为
> 未达验收。本文档所述"前端/Mock/缺密钥"行为均有代码级验证（见 §8），完整容器实机验收需在
> 具备 Docker 的服务器上按 §3-§6 执行。

---

## 0. 目标与验收口径

| 目标 | 验收方式 |
|---|---|
| 服务器仅开放 80/443 | `healthcheck.sh` 第 [1] 项：宿主编译 Port 只含 80/443 |
| API 可经 HTTPS 同源访问 | 第 [2] 项：`curl -k https://host/` 与 `https://host/api/openapi.json` 均 200，且 API 无 CORS 头 |
| 数据库/Redis 无公网监听 | 第 [3] 项：宿主无 5432/6379 监听；`compose port` 无业务端口发布 |
| Mock 认证或缺少 JWT 密钥时 fail-closed | 第 [4] 项 / §8：受限环境 `create_app` 启动即抛 `RuntimeError` |

预览拓扑要点（与 `docker-compose.preview.yml` 对应）：

- **暴露面**：仅 `nginx` 加入 `edge` 网络并发布 **80/443**；`postgres/redis/api/frontend/worker/migrate`
  全部只在 `internal` 私网，**不发布任何宿主机端口**（宿主机不可达业务端口）。
- **HTTPS + 同源**：nginx 终结 TLS，`/api/*` → `api:8000`，其余 → `frontend:3000`；浏览器只访问同源
  `https://host/api`，故 API `ENABLE_CORS=false`。SSE（`/api/chat`）已关闭代理缓冲并加长超时。
- **密钥注入**：所有演示密码/示例密钥移除，统一由服务器环境密钥注入（`deploy/.env.preview`）。
- **角色分离**：迁移角色（`POSTGRES_USER=migrator`，owner/DDL）与运行角色（`app_runtime`，
  `LOGIN NOINHERIT NOSUPERUSER NOBYPASSRLS`，仅 DML）严格分离；`api/worker` 仅以运行角色连接
  （`DATABASE_URL`），且 `DATABASE_MIGRATE_ON_STARTUP=false`，不执行 DDL。
- **fail-closed**：`ENV=preview` 下 `AUTH_BACKEND=mock`、或 `AUTH_BACKEND=real` 但缺 `AUTH_JWT_SECRET`，
  `create_app` 启动即抛 `RuntimeError`（见 `src/main.py::fail_closed_auth_guard`）。

---

## 1. 机密清单（须由服务器环境注入）

复制 `deploy/.env.preview.example` → `deploy/.env.preview`，逐项替换为真实机密（已 gitignore）：

| 变量 | 用途 | 缺失后果 |
|---|---|---|
| `POSTGRES_PASSWORD` | PostgreSQL owner/迁移角色口令 | postgres 启动失败 |
| `REDIS_PASSWORD` | Redis `requirepass` | redis 启动失败 |
| `APP_RUNTIME_PASSWORD` | 运行角色 `app_runtime` 口令 | 无法迁移角色；api/worker 认证失败 |
| `AUTH_JWT_SECRET` | JWT HS256 密钥 | 受限环境 api 启动即 fail-closed |
| `AUTH_JWT_ISSUER`/`AUTH_JWT_AUDIENCE` | JWT 固定声明 | 签发/校验必须一致，不匹配一律拒绝 |
| `AUTH_JWT_TTL_SECONDS` | JWT 固定过期（秒） | 签发 `exp = iat + 该值`；缺省 3600 |
| `AUTH_JWT_KID` | 当前密钥 kid（可选） | 留空自动派生；JWT header 携带 |
| `AUTH_JWT_ROTATED_SECRETS` | 上个密钥（轮换池，逗号分隔） | 仅校验旧令牌；未知 kid 一律拒绝 |
| `AUTH_LOGIN_CREDENTIALS` | 登录凭据表 JSON（`{tenant:user: sha256}`） | 未配置则 `/api/auth/login` 登录 fail-closed |
| `LLM_API_KEY` | 自托管 OpenAI 兼容端点密钥 | 预览校验失败 |
| `LLM_BASE_URL`/`LLM_MODEL` | 内网模型端点 | 需自托管可达 |
| `LLM_ALLOWED_HOSTS` | 端点网络白名单（host/IP/CIDR，逗号分隔） | 受限环境留空 → 端点访问 fail-closed（阻断未批准外联） |
| `HIGH_CONFIDENCE_MODELS` | 写操作能力矩阵白名单（仅评测通过的 model id） | 留空或含未评测模型 → 写操作 fail-closed 转人工 |
| `LLM_EVAL_REPORT_PATH` | 写操作专项评测报告路径 | 受限环境缺失 → 白名单不生效（无模型可写） |

> 机密不得提交、不得出现在 `deploy/.env.preview.example`（仅填 `<inject>` 占位）。`deploy/.env.preview`
> 与 `deploy/secrets/certs/` 已加入 `.gitignore`。

---

## 2. 部署（一次性，幂等）

在仓库根目录、具备 Docker 的服务器上执行：

```bash
# 0) 注入机密（见 §1）
cp deploy/.env.preview.example deploy/.env.preview && $EDITOR deploy/.env.preview
# 自检机密是否齐全（缺失即失败，fail-closed）
bash deploy/scripts/check_secrets.sh

# 1) 生成 TLS 证书（预览自签名；生产替换为受信 CA）
bash deploy/scripts/gen_certs.sh

# 2) 构建并启动（前置依赖自动等待；后端镜像当前 git 引用）
bash deploy/scripts/deploy.sh

# 3) 查看状态
docker compose --env-file deploy/.env.preview -f docker-compose.preview.yml ps
```

`deploy.sh` 等价于：`check_secrets → gen_certs → compose build → compose up -d → wait nginx healthy`，
并把本次 git 引用追加到 `deploy/backups/rollback.log`。

---

## 3. 迁移（可重复执行，幂等）

数据库 DDL（业务表 + 官方 checkpoint 表 + RLS + 函数 + 运行角色）只由 `migrate` 一次性服务执行，
使用**迁移角色**连接：

```bash
bash deploy/scripts/migrate.sh
```

等价于：`compose run --rm migrate`（其 `command` 为 `python -m src.infrastructure.migrate_cli`）。

要点：

- `migrate_cli` 优先读取 `DATABASE_MIGRATOR_URL`（迁移角色），**绝不回退到运行角色**执行 DDL；
  受限环境下若两者相同会直接拒绝（`SystemExit`）。
- `apply_runtime_role` 用 `APP_RUNTIME_PASSWORD` 创建/更新 `app_runtime` 并 GRANT 表/序列/函数权限。
- 迁移幂等：`CREATE TABLE IF NOT EXISTS` + `CREATE OR REPLACE FUNCTION` + `DROP POLICY ... CREATE POLICY`，
  可重复执行。
- **运行角色不做 DDL**：`api/worker` 设 `DATABASE_MIGRATE_ON_STARTUP=false`；其 `lifespan` 只做
  `require_postgres_ready`（RLS 自检），未就绪即 fail-closed。

---

## 4. 回滚（数据库恢复 + 应用重建）

安全优先：向前迁移本身幂等，回滚的重点是**恢复数据（RPO）**与**应用版本回退**。不提供"回退到中间
schema"，与生产基线"先备份、变更、再恢复靠快照"一致。

```bash
# 0) 回滚前快照（可逆性）
bash deploy/scripts/backup_db.sh deploy/backups/pre-rollback-$(date +%Y%m%d%H%M%S).dump

# 1) 恢复到变更前的数据快照（若已知），并重建应用到变更前 git 引用
bash deploy/scripts/rollback.sh deploy/backups/langgraph-<ts>.dump <git-ref>
```

`rollback.sh` 流程：先 `pg_dump` 快照现状 → `pg_restore --clean --if-exists` 恢复到指定备份 →
（可选）`git checkout <ref>` 并 `compose build + up -d`。

若仅需**数据**回滚：`bash deploy/scripts/rollback.sh deploy/backups/<file>.dump`（不传 git-ref）。
若需**版本**回退：先 `bash deploy/scripts/deploy.sh <git-ref>`（按固定引用重建），再恢复数据。

---

## 5. 健康检查（验收）

```bash
bash deploy/scripts/healthcheck.sh
```

逐项核验（见 §0 验收口径）：

1. 宿主仅开放 80/443（`ss`/`netstat` 枚举；出现 5432/6379/8000/3000 即违规）。
2. HTTPS 同源：`https://host/` 与 `https://host/api/openapi.json` 均 200；API 响应无 `Access-Control-Allow-Origin`。
3. 数据库/Redis 无公网监听：宿主无 5432/6379；`compose port postgres 5432` 等为空。
4. Mock / 缺 JWT 密钥 fail-closed：`verify_fail_closed.sh`（在一次性 `api` 容器内执行）。

任意一项失败即非零退出（fail-closed 闸门）。

---

## 6. 日常运维 / 观测

- 查看日志：`docker compose --env-file deploy/.env.preview -f docker-compose.preview.yml logs -f api nginx`
- 定期备份：`bash deploy/scripts/backup_db.sh`（RPO 路径；建议结合 crontab）。
- 模型/审批/审核：受限环境 fail-closed 只影响 `api`；`worker`（Celery）不触发 `create_app`，安全。
- 严格完全自托管出口阻断（可选）：把 `internal` 网络设为 `internal: true`，并把自托管 LLM 网关
  挂到该网络；此时容器无法访问外网，符合"未经批准外联阻断"。

---

## 7. 与本地开发拓扑的边界

- `docker-compose.yml`：本地/单机功能验证，仍用 `AUTH_BACKEND=mock`、公开业务端口、演示密码——
  **仅限本地**，不得对外。preview 与生产一律用 `docker-compose.preview.yml`。
- `docker-compose.preview.yml`：线上预发布，`AUTH_BACKEND=real`、`STORAGE_BACKEND=postgres`、
  `ENABLE_CORS=false`、仅 80/443、密钥注入、角色分离；compose 项目名 `after-sales-preview`、
  PostgreSQL 数据卷 `./data/preview-postgres`，与本地 compose 与数据卷相互隔离。
- 两者互斥：绝不把演示密码/mock 认证带入 preview。

---

## 8. 代码级验证已执行（本仓库）

- `scripts/verify_preview_fail_closed.py`（venv）→ `EXIT=0`，6 项全过：
  preview/production + mock、preview/production + real + 缺密钥 均启动即抛 `RuntimeError`；
  preview + real + 提供密钥、development + mock 允许构建。
- pytest：`tests/test_security_regressions.py` 17 passed（含新增
  `test_create_app_fails_closed_on_missing_jwt_in_restricted_env`）。
- 全量（非 postgres 标记）：`109 passed, 1 skipped, 12 deselected`（无回归）。

> 说明：本会话环境无 Docker，未实机运行 `docker-compose.preview.yml`；容器实机验收需在目标服务器按
> §2-§5 执行。前端（Next.js）与后端 `src/` 的同源/代理/密钥注入均已按上述配置就位。

---

## 9. 本阶段：自托管 LLM 端点接入 + 评测驱动能力矩阵（前置排除项）

本阶段上线**不依赖** Graphiti、Neo4j、Milvus、CrewAI；下列项为此阶段的上线前置条件与验收口径：

| 前置条件 | 说明 | 验证方式 |
|---|---|---|
| 自托管 LLM 端点 | 内网 OpenAI 兼容端点（vLLM/Ollama/项目自管服务），完全自托管、不接公有 SaaS | `GET /v1/models` 200；`scripts/verify_llm_chain.py [A]` |
| 端点网络白名单 | `LLM_ALLOWED_HOSTS` 限定端点 host/IP/CIDR；受限环境留空即 fail-closed | `tests/test_llm_endpoint_gate.py::test_endpoint_guard_*`；`EndpointGuard` |
| 超时 + 有界重试 | 单调用 deadline（`LLM_TIMEOUT_SECONDS`）+ 同类型错误有界重试 + 指数退避 | `OpenAICompatibleLLM._chat`；5xx/超时 → `LLMUnavailableError` |
| 脱敏日志 | `LLM_LOG_REDACT` 打开；掩码手机号/地址/订单号/密钥/授权头 | `test_redact_masks_pii` / `test_redact_url_masks_query` |
| 写操作能力矩阵（评测驱动） | 只有写操作专项评测 `write_op_pass=true` 的 model id 进入 `HIGH_CONFIDENCE_MODELS` | `capability.resolve_high_confidence_models`；`tests/test_llm_endpoint_gate.py` 竞赛块 |
| 异常/超时不触发执行 | LLM 5xx/超时/非法输出 → 写操作 fail-closed 转人工，不产生 approval/执行 | `test_endpoint_5xx_raises_unavailable` / `test_timeout_raises_unavailable` / `test_non_whitelist_*` |
| 未白名单模型不达审批执行链 | 非白名单模型退款/退货/改址 → SSE 仅 `error`（`model_not_in_whitelist`），`operation_id=null` | `test_non_whitelist_model_write_fails_closed_via_api` |

**前置排除项（本阶段**不**做，不构成上线阻塞）**：Graphiti/Neo4j（长期记忆图谱）、Milvus（向量检索）、
CrewAI（子智能体）均暂不作为本阶段上线前置条件。政策问答在本阶段走确定性 keyword 检索 + RAG 忠实度
校验（`LLM_MODEL` 经评测后可启用）；Milvus/CrewAI 属后续迭代项。

**评测/验收脚本**（venv）：
```bash
# 候选模型评测（产出写操作专项报告）
python scripts/evaluate_models.py --llm-backend openai_compatible \
    --model-name <model-id> --base-url http://<endpoint>/v1 \
    --output evidence/llm_candidate_eval.json
# 三个验收项（真实链路 / 异常不执行 / 非白名单不达审批链）
python scripts/verify_llm_chain.py
```

---

## 10. 本阶段新增：自托管观测链路 + 预发布演练（配套交付）

| 交付物 | 路径 | 用途 |
|---|---|---|
| 自托管可观测栈 | `docker-compose.observability.yml` + `deploy/observability/*.yml` + `deploy/scripts/observability.sh` | Langfuse(trace) + Prometheus/Grafana(指标) + Loki/Promtail(日志)，完全自托管 |
| 全局 PII 脱敏日志总线 | `src/observability/logging.py` | 订单地址/支付信息/密钥等在**所有 logger** emit 前脱敏；结构化日志带 tenant 上下文 |
| Langfuse trace | `src/observability/tracing.py` | metadata 写 `tenant_id/session_id/environment`，输入输出脱敏；未配置即 no-op |
| Prometheus 指标 | `src/observability/metrics.py` + `GET /api/metrics` | 有界标签（禁用高基数 `tenant_id`） |
| 审计四维追溯 | `store.search_audit` + `GET /api/audit` + `scripts/audit_trace.py` | 按租户/会话/审批/operation_id 追溯，detail 脱敏 |
| 首次上线门控 | `src/core/launch_gate.py` + `LAUNCH_ALLOWED_TENANTS` + `scripts/verify_launch_gate.py` | 少量租户/仅审批后执行/全量审计/人工复核；受限环境 `--strict` fail-closed |
| 预发布演练 | `deploy/drills/`（`run_preview_drills.sh` + `drill_*.sh` + `drill_api.sh` + `verify_dev_drills.py` + `record_template.md`） | 六项演练（重启/备份恢复/审批/SSE/并发/对账），产出可复核记录 |
| 运行手册 | `deploy/OPS_RUNBOOK.md` | 上线阈值、回滚条件、值班流程、每日备份恢复演练记录、首启约束 |

**本会话验证**：`pytest -m "not postgres"` → **176 passed, 1 skipped**（新增观测链/门控/审计/指标/演练测试）；
`python deploy/drills/verify_dev_drills.py` → **6/6 PASS**（本环境可运行的 D1–D6 等价子集，记录见
`deploy/drills/records/dev-drills-*.json`）；`scripts/verify_launch_gate.py --strict` 门控 PASS；
`scripts/audit_trace.py` 四维追溯 + 脱敏通过。

> 说明：本会话环境无 Docker、无 SSH 主机，**服务器实机**部分以可直接执行的脚本 + 记录模板交付；
> 本地可运行子集已实际运行通过并生成记录。

*与《生产环境架构设计》§4、《生产基线与验收测试》§五、《Agent 宪法》第一层安全红线配套。*
