# 目标服务器预发布硬化（网络/证书/密钥/可观测/租户接入清单）— deploy-engineer · 阶段四

> 角色：deploy-engineer（沙箱网关 / Docker Compose 运行验证工程师）· rc4-productionization · 任务 t5
> 环境：Docker Engine 29.7.2 已连接；本机即此工作区，**目标生产服务器（外部资产）未接入**。
> 铁律：凡依赖外部输入（受信 CA/真实域名、真实资金渠道、书面确认真实租户、目标服务器主机防火墙、观测链路密钥）未到位，一律 `BLOCKED-需外部`；**绝不把静态审查/配置解析写成生产实测**。本报告为「目标服务器预发布硬化 **就绪评估 + 上线 runbook + 清单**」，并提供本次在本机可做的有限核验。
> 交叉引用：`evidence/prod-go-live/deploy-engineer/INFRA_READINESS_GAP.md`（阶段2/3/5/7）、`SANDBOX_GATEWAY_RUNTIME_VERIFY.md`（阶段3 运行态，已由我于真实 Docker 复核）、`deploy/{EGRESS_POLICY,TLS_TRUSTED_CERTS,OPS_RUNBOOK,PREVIEW_DEPLOYMENT_CHECKLIST,DR_KEY_MANAGEMENT}.md`。

---

## 0. 一次结论

生产栈的**配置侧弹性/自托管红线**已基本就绪（凭证 fail-closed、数据面不发布端口、仅 nginx 出公网、check_secrets 门控、边界3 已被我运行态复核）。但**目标服务器预发布硬化 5 轴中有 4 轴 BLOCKED-需外部**：受信 CA（现自签）、生产观测链路密钥缺失（observability compose `config` FAIL）、网关密钥缺失（prod compose `config` FAIL）、真实租户书面确认（tenants.json 仅示例）。**本次实际在运行态/配置级复核的：网络暴露面 + 证书自签 + 生产 compose/observability 的密钥缺口计数。** 下表为诚实就绪矩阵。

| 轴 | 当前就绪度 | 本次核验 | 状态 |
|:---:|:---:|:---|:---:|
| 网络/出网 | 仅 nginx 发布 80/443（prod 用 8080/8843）；数据面全部 internal-only | prod compose 只有 nginx 有 `ports:`；EGRESS_POLICY 允许清单/主机防火墙清单成文 | ⚠️ 配置 ✅ / **主机防火墙=部署期（BLOCKED）** |
| 证书/TLS | 自签 `CN=preview.local`（subject==issuer），`2026/9/4→2027/9/4` | certutil dump 复核自签 | ❌ **BLOCKED-需受信 CA** |
| 密钥 | check_secrets.sh fail-closed 门控存在；但 prod compose 缺 `GATEWAY_API_KEY`/`SANDBOX_GATEWAY_API_KEY` | `docker compose config`(prod) FAIL(exit 1)；`check_secrets.sh` 存在 | ❌ **BLOCKED-需注入网关密钥** |
| 可观测 | 观测 compose 配置在 preview env 下 `config` 通过(EXIT=0)；**production env 缺 9 项密钥 → config FAIL**；应用侧 `LANGFUSE_PUBLIC_KEY/SECRET_KEY` 为空 → trace=NO-OP | obs compose(prod env) FAIL；preview env EXIT=0 | ❌ **BLOCKED-需注入观测密钥 + 开放 trace** |
| 租户接入清单 | `tenants.json` 为**示例**（ACME-RETAIL/GLOBEX-ECOM，明示非真实）；角色限 customer/agent/admin/approver；创建走受控脚本（禁 seed_default） | 静态审查 | ❌ **BLOCKED-需业务方书面确认真实租户** |

---

## 1. 网络 / 出网（Egress）

**已核验（本机运行态/配置级）**
- `docker-compose.prod.yml` 中只有 `nginx` 发布宿主端口：`ports: ["8080:80","8843:443"]`；`postgres`/`redis`/`migrate`/`api`/`worker`/`frontend`/`backup`/`sandbox-gateway` 均**无 `ports:`** → 数据面 + 应用面仅内网可达（`after-sales-prod_internal` 172.31.0.0/16）。
- 网关无宿主端口（`SANDBOX_GATEWAY_RUNTIME_VERIFY.md`：`PortBindings={}`、`Ports={"8000/tcp":null}`）。
- 出网立场/允许清单见 `deploy/EGRESS_POLICY.md`（默认无出网、无 `network_mode: host`、LLM/Metrics 内网白名单、`internal:true` 在 Docker Desktop 会破坏内嵌 DNS 的回退说明）。

**目标服务器 runbook（部署期执行）**
1. 主机防火墙（iptables/nftables）仅放行宿主机 → `nginx` 的 80/443；放行内网网段容器间通联；**默认 DROP** 容器网段 → 公网/其它网段外联。
2. 对容器→公网 NAT 出站，仅放行到 `METRICS_ALLOWED_SOURCES` / `LLM_ALLOWED_HOSTS` 内的内网地址，其余拒绝。
3. `METRICS_ALLOWED_SOURCES` 按目标服务器实际内网子网收敛（prod 默认 172.31.0.0/16 与 `after-sales-prod_internal` 对齐；若改网段需同步）。
4. 公网访问 `/api/metrics` 必须 404（nginx `location = /api/metrics { return 404; }`），内网 Prometheus 直连 `api:8000/api/metrics` 必须命中白名单返回 200。
5. 复核仅 8080/8843 暴露到公网；`LLM_BASE_URL` 保持内网、`LLM_ALLOWED_HOSTS` 只含内网 host/CIDR（受限环境空则 fail-closed）。

**状态**：✅ 配置/网络暴露面已核验；❌ 主机防火墙策略 = 目标服务器部署期（BLOCKED-需外部服务器）。

---

## 2. 证书 / TLS

**已核验（本机）**：`certutil -dump deploy/secrets/certs/server.crt` → `CN=preview.local`；`Subject==Issuer`（自签，subject==issuer）；`NotBefore 2026/9/4`、`NotAfter 2027/9/4`。**受信 CA 未就绪（BLOCKED）。**

**目标服务器 runbook**
1. 申请生产域名 + 受信 CA 证书链（fullchain + 私钥），替换 `deploy/secrets/certs/{server.crt,server.key}`；私钥权限 0600、不入 Git/镜像。
2. `nginx -t` + 端到端 HTTPS 复核（curl -kv，校验证书链/主机名/吊销）。`deploy/scripts/verify_tls.sh` 可复用。
3. nginx 强制可加 HSTS/安全头；`server_name` 用真实域名（`prod.conf`）。
4. 证书轮换（`DR_KEY_MANAGEMENT.md`）与到期监控入告警。

**状态**：❌ **BLOCKED-需受信 CA + 生产域名**（自签不可作为生产 TLS 证据）。

---

## 3. 密钥（Key/Secret hardening）

**已核验（本机）**
- `deploy/scripts/check_secrets.sh`：存在，fail-closed 门控（缺/占位/命中被禁默认值即拒绝部署）。覆盖 POSTGRES/REDIS/APP_RUNTIME/BACKUP 口令、AUTH_JWT_SECRET、AUTH_LOGIN_CREDENTIALS、LLM_API_KEY、LLM_ALLOWED_HOSTS、HIGH_CONFIDENCE_MODELS、LAUNCH_ALLOWED_TENANTS、EXECUTION_CALLBACK_HMAC_SECRET；禁止 `shadow-callback-secret` 默认值；受限环境强制 `LAUNCH_GATE_STRICT=true`；登录凭据禁明文/无盐 sha256。
- **生产 compose `config` FAIL（exit 1）**：`required variable GATEWAY_API_KEY / SANDBOX_GATEWAY_API_KEY is missing`（注入这两项后才能拉起含 `sandbox-gateway` 的生产栈）。→ 见 `SANDBOX_GATEWAY_RUNTIME_VERIFY.md` §3（compose 代码就绪、env 配置缺口）。
- `deploy/.env.production` 亦未注入观测密钥（见 §4）、`LLM_BACKEND=mock`、`HIGH_CONFIDENCE_MODELS=__NO_VERIFIED_WRITE_MODEL__`（fail-closed 安全占位）。

**目标服务器 runbook**
1. 为 `deploy/.env.production` 注入：`GATEWAY_API_KEY`、`SANDBOX_GATEWAY_API_KEY`（沙箱网关两把密钥），并设 `EXECUTION_PROVIDER=sandbox_http`（当前=mock）。
2. `bash deploy/scripts/check_secrets.sh --env production` 通过；`docker compose --env-file deploy/.env.production config --quiet` 通过。
3. `AUTH_JWT_SECRET`/`EXECUTION_CALLBACK_HMAC_SECRET`/`BACKUP_ENC_KEY` 等用 CSPRNG 随机；`AUTH_LOGIN_CREDENTIALS` 用 argon2id PHC；`AUTH_CREDENTIAL_HASH=argon2id`。
4. 密钥不入 Git（`.env.production` 已被 `.gitignore` 且 `git ls-files` 为空——KEY_STATE_AUDIT）；日志 `LLM_LOG_REDACT=true` 强凭据不落日志。

**状态**：✅ 门控/策略成文；❌ 生产网关密钥未注入 → prod compose `config` FAIL（BLOCKED-需注入两把网关密钥 + 切 sandbox_http）。

---

## 4. 可观测（Observability）

**已核验（本机）**
- `docker-compose.observability.yml` 配置：Langfuse（trace）、Prometheus+Grafana（指标）、Loki+Promtail（日志），全内网（`obs` 网 `internal:true`），MinIO 对象存储，`LANGFUSE_*`/`GRAFANA_ADMIN_PASSWORD` 等均 `${:?}` fail-closed。
- `docker compose --env-file deploy/.env.preview -f docker-compose.observability.yml config --quiet` → **EXIT=0**（preview 观测栈配置可用）。
- `docker compose --env-file deploy/.env.production -f docker-compose.observability.yml config --quiet` → **EXIT=1**，缺失 9 项必需密钥：`GRAFANA_ADMIN_PASSWORD`、`LANGFUSE_CLICKHOUSE_PASSWORD`、`LANGFUSE_DB_PASSWORD`、`LANGFUSE_ENCRYPTION_KEY`、`LANGFUSE_MINIO_ROOT_PASSWORD`、`LANGFUSE_MINIO_ROOT_USER`、`LANGFUSE_NEXTAUTH_SECRET`、`LANGFUSE_REDIS_PASSWORD`、`LANGFUSE_SALT` → **生产观测栈不可拉起**。
- 应用侧 trace：`deploy/.env.production` 与 `deploy/.env.preview` 的 `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` 均为 **空（len 0）** → **trace=NO-OP**（`observability-engineer/OBSERVABILITY_REPORT.md` §4 已判定，切 live 前必须补齐）。

**目标服务器 runbook**
1. 注入生产观测密钥（上述 9 项 + MinIO ROOT_USER/PASSWORD）。
2. 注入 `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` 到 `deploy/.env.production`（api/worker 生效），开启 trace 落地并验证（Langfuse 收到带 `tenant_id/session_id/environment` 的 trace；PII 脱敏）。
3. 按顺序拉起 observability 栈（先 preview/prod 栈、后 obs 栈，避免子网冲突）；Prometheus 经 `prod-net`（复用 `after-sales-prod_internal`）直连 `after-sales-prod-api-1:8000/api/metrics`，来源命中 `METRICS_ALLOWED_SOURCES=172.31.0.0/16`。
4. 告警规则（`deploy/observability/alert-rules.yml`）与 `reconcile_last_run_timestamp_seconds` gauge（对账超时未运行）端到端复验；`alertmanager`/`webhook-sink` 通。**完全自托管**：`TELEMETRY_ENABLED=false`；无未批准出网。

**状态**：❌ **BLOCKED-需注入生产观测密钥 + 开放 trace**（preview 观测栈配置可用；生产观测栈 `config` FAIL、应用 trace NO-OP）。

---

## 5. 租户接入清单（Tenant onboarding checklist）

**已核验（本机，静态）**：`deploy/records/tenants.json` 为**示例**（`ACME-RETAIL`/`GLOBEX-ECOM`，`_template_note` 明示「示例租户与成员**非真实**，切勿以本示例直接写入生产」）。角色仅限 `customer/agent/admin/approver`；`platform_admin` 为独立平台能力，不得写为租户成员角色。preview/production 禁用 seed_default（`DEMO_SEED_ENABLED=false`）。创建走受控脚本（`scripts/create_bootstrapped_tenants.py --spec <json> --dry-run`）。

**目标服务器（业务方确认后）runbook**
1. 业务方签署确认函（`CANARY_TENANTS-*.md` / `CANARY_TENANT_CONFIRMATION_TEMPLATE.md` 模板）：真实 `tenant_id`、名称、成员/角色（admin/approver/agent/customer）。
2. 用 `argon2id` PHC 生成登录凭据，注入 `AUTH_LOGIN_CREDENTIALS`；`LAUNCH_ALLOWED_TENANTS` 注入首批白名单；`HIGH_CONFIDENCE_MODELS` 注入通过评测的写模型（防全量转人工）。
3. `create_bootstrapped_tenants.py --spec <real-json> --dry-run` 校验 → release-manager 以受控方式执行创建（不 seed_default）。
4. 审计留痕：成员/角色授予、租户生命周期（停用即阻断新请求与后台任务）、被遗忘权（删实体+关联事实），按租户范围 + 记录操作者/依据/时间/结果。
5. 每租户做一次影子（shadow）提交 + 对账复核（`EXECUTION_MODE=shadow`），再按下文门控评估是否切 live。

**状态**：❌ **BLOCKED-需业务方书面确认真实租户**（当前 0 个真实租户，`LAUNCH_ALLOWED_TENANTS=__NONE_APPROVED_YET__`；系统处 S1 shadow fail-closed 安全默认态）。

---

## 6. 诚实结论（绝不伪造）

1. 本次在**本机运行态/配置级**复核：网络暴露面（仅 nginx 出公网、数据面 internal-only）、证书自签（CN=preview.local）、生产 compose `config` FAIL（缺网关密钥）、生产观测 compose `config` FAIL（缺 9 项观测密钥）、应用 trace NO-OP、`check_secrets.sh` 门控存在、`tenants.json` 为示例。
2. **目标服务器硬化 5 轴中 4 轴 BLOCKED-需外部**：受信 CA/生产域名、生产网关密钥、生产观测密钥+trace、业务方书面确认真实租户。主机防火墙策略亦为目标服务器部署期项。
3. **能实测/已实测的（非外部依赖）**：网络暴露面、证书自签属性、生产/观测 compose 的密钥缺口计数与本机可用性——均以真实命令/配置核验，**未把 mock/配置解析写成生产实测**。
4. 一切「生产已硬化/已放量」判定，以**目标外部输入到位 + 部署期执行项完成**为前件；当前系统在 **S1 shadow / fail-closed** 安全默认态（无真实租户放行、无写白名单模型、execution 不触真实资金）。

---

*证据来源：`docker-compose.prod.yml`、`docker-compose.observability.yml`、`deploy/.env.production`、`deploy/.env.preview`、`deploy/scripts/check_secrets.sh`、`deploy/secrets/certs/server.crt`（certutil 复核）、`deploy/EGRESS_POLICY.md`、`deploy/TLS_TRUSTED_CERTS.md`、`deploy/OPS_RUNBOOK.md`、`deploy/records/tenants.json`、`deploy/records/CANARY_TENANTS-*.md`、`evidence/prod-go-live/deploy-engineer/{SANDBOX_GATEWAY_RUNTIME_VERIFY.md,INFRA_READINESS_GAP.md}`、`observability-engineer/OBSERVABILITY_REPORT.md`。全程只读；未修改任何密钥/证书/生产配置，未部署到目标服务器。*
