# PROD_STACK_HEALTH — 独立生产栈（after-sales-prod）Compose 迁移 + 健康复验

> 角色：deploy-engineer（T7）· 关联 t2（迁移硬化修复）
> 目的：用当前工作树（含 `shipping_events` 复合外键修复）通过 **docker compose** 迁移服务 + 完整生产栈 bring-up，确认干净迁移 exit 0、三表+RLS+角色就绪、全容器健康。

## 关键修复点
- 迁移缺陷已修复：`src/infrastructure/migrations.py::shipping_events` 改为表级复合外键 `FOREIGN KEY (tenant_id, order_id) REFERENCES orders(tenant_id, order_id) ON DELETE CASCADE`（提交 `696444a`）。
- api 启动另需受限环境强制项：`BUSINESS_DATA_BACKEND=postgres`（launch gate fail-closed，mock 仅限测试）——已加入 `deploy/.env.production` 与 `docker-compose.prod.yml` 的 api/worker env。

## 迁移（compose migrate 服务）
命令：`docker compose --env-file deploy/.env.production -f docker-compose.prod.yml up -d`
- **`after-sales-prod-migrate-1` exit = 0**，日志：`migrations applied; runtime role and backup role ensured.`

## 全栈容器状态（`docker compose ps`，全部健康）
```
after-sales-prod-api-1        Up  (healthy)   8000/tcp
after-sales-prod-backup-1     Up              5432/tcp
after-sales-prod-frontend-1   Up  (healthy)   3000/tcp
after-sales-prod-nginx-1      Up  (healthy)   0.0.0.0:8080->80/tcp, 0.0.0.0:8843->443/tcp
after-sales-prod-postgres-1   Up  (healthy)   5432/tcp
after-sales-prod-redis-1      Up  (healthy)   6379/tcp
after-sales-prod-worker-1     Up  (healthy)   8000/tcp
```
（`migrate` 为一次性服务，exit 0 后退出。）

## 健康端点（经 nginx 独立宿主端口 8080/8843）
| 端点 | 路径 | 结果 |
|------|------|------|
| nginx /healthz | `http://127.0.0.1:8080/healthz` | `200` |
| api /api/healthz | `https://127.0.0.1:8843/api/healthz`（自签 -k） | `200`，body=`{"status":"ok"}` |
| frontend / | `https://127.0.0.1:8843/`（自签 -k） | `200` |
| /api/metrics 公网 | `https://127.0.0.1:8843/api/metrics` | `404`（内网化，正确阻断） |

## 库/RLS/角色/外键（生产 postgres `after_sales_prod`，owner=migrator）
- 三表存在：`orders`、`policy_documents`、`shipping_events`。
- `shipping_events` 外键：
  - `shipping_events_tenant_id_fkey | FOREIGN KEY (tenant_id) REFERENCES tenants(id)`
  - `shipping_events_tenant_id_order_id_fkey | FOREIGN KEY (tenant_id, order_id) REFERENCES orders(tenant_id, order_id) ON DELETE CASCADE`（**复合外键，正确**）
- RLS：`orders/policy_documents/shipping_events` 均 `enabled=true forced=true policies=1`。
- 角色：`app_runtime`（inherit=false super=false createdb=false createrole=false canlogin=true）、`backup_role`（同）→ 最小权限、NOBYPASSRLS 语义运行/备份角色。
- 外键完整性实测：`INSERT tenants('P-A') → orders('P-A','O-1') → shipping_events('P-A','O-1')` 全部成功，返回 `FK-OK`。

## 结论
- 独立生产栈（project=after-sales-prod）**全部容器健康**；迁移 exit 0；三表 + RLS FORCE + 租户 policy + app_runtime/backup_role + 复合外键全部就绪；api `/api/healthz` 200。
- preview 栈未受影响（7 容器仍运行）。
- 边界：nginx 443 当前用**自签**证书（非受信，TLS **BLOCKED-需外部**）；LLM_BACKEND=mock（真实 LLM 评测/端点 BLOCKED-需外部）；`BUSINESS_DATA_BACKEND=postgres`（读空表 → 无数据时 fail-closed 转人工，符合生产前无放行租户姿态）。EXECUTION_MODE=shadow（不触真实资金）。
