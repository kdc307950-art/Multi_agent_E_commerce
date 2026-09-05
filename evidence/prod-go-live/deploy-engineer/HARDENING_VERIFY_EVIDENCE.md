# 硬化核验证据（deploy-engineer · 阶段四 t5 实跑记录）

> 本文件记录 t5「目标服务器预发布硬化」中我在**本机 Docker 引擎环境**实际执行的核验命令与结果（非目标服务器，目标服务器为外部资产未接入）。用于支撑 `TARGET_SERVER_PRE_RELEASE_HARDENING.md` 的结论。

## 1. 环境
```
docker version --format '{{.Server.Version}}'        → 29.7.2（Docker Engine 已连接）
docker compose version                                → Docker Compose v5.5.0
```

## 2. 网络暴露面（prod compose：哪些服务发布宿主端口）
命令（只提取 `ports:` 行）：`Select-String docker-compose.prod.yml -Pattern '^\s*ports:|^\s*- "\d+:\d+'`
结果（仅 nginx 发布）：
```
ports:
- "8080:80"
- "8843:443"
```
→ `postgres/redis/migrate/api/worker/frontend/backup/sandbox-gateway` 均无 `ports:`（internal-only）。

## 3. 证书自签核验
命令：`certutil -dump deploy/secrets/certs/server.crt`
结果：
```
CN=preview.local
NotBefore: 2026/9/4 2:31
NotAfter: 2027/9/4 2:31
CN=preview.local        # Subject == Issuer → 自签（受信 CA 未就绪）
```

## 4. 生产 compose config 核验（encrypted keys fail-closed）
命令：`docker compose --env-file deploy/.env.production -f docker-compose.prod.yml config --quiet`
结果：`EXIT=1`，缺失必需变量：
```
required variable GATEWAY_API_KEY is missing
required variable SANDBOX_GATEWAY_API_KEY is missing
```
（`deploy/.env.production` 同时 `EXECUTION_PROVIDER=mock`——覆盖 compose 默认 sandbox_http。）

## 5. 观测栈 compose config 核验
```
docker compose --env-file deploy/.env.preview -f docker-compose.observability.yml config --quiet   → EXIT=0（preview 可用）
docker compose --env-file deploy/.env.production -f docker-compose.observability.yml config --quiet → EXIT=1（production 缺 9 项）
```
production 缺失：`GRAFANA_ADMIN_PASSWORD, LANGFUSE_CLICKHOUSE_PASSWORD, LANGFUSE_DB_PASSWORD, LANGFUSE_ENCRYPTION_KEY, LANGFUSE_MINIO_ROOT_PASSWORD, LANGFUSE_MINIO_ROOT_USER, LANGFUSE_NEXTAUTH_SECRET, LANGFUSE_REDIS_PASSWORD, LANGFUSE_SALT`。

## 6. 应用侧 Langfuse trace
`deploy/.env.production` 与 `deploy/.env.preview` 的 `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` 均 **len 0（空）** → trace=NO-OP。

## 7. 密钥门控脚本存在性
`deploy/scripts/check_secrets.sh` 存在（fail-closed：缺/占位/命中被禁默认值即拒绝部署；禁止 `shadow-callback-secret`；受限环境强制 `LAUNCH_GATE_STRICT=true`；登录凭据禁明文/无盐 sha256）。

## 8. 租户清单
`deploy/records/tenants.json` = 示例（ACME-RETAIL/GLOBEX-ECOM，明示非真实）；角色限 customer/agent/admin/approver；preview/production 禁用 seed_default。

---

*以上均为本机可执行核验；凡涉及目标服务器主机防火墙、受信 CA/生产域名、生产观测密钥+trace、业务方书面确认真实租户的，均为外部输入/部署期执行项 → BLOCKED-需外部。*
