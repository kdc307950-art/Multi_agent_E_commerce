# MIGRATE_VERIFY — 全新 prod-like 库 clean migrate 复验（t2 硬化修复项）

> 角色：deploy-engineer · 关联：t2（security-auditor 发现）/ t1（本基线与 Go/No-Go）
> 目的：证明 `src/infrastructure/migrations.py` 的 `shipping_events` 复合外键修复后，**全新** PostgreSQL 行内迁移成功，无 `InvalidForeignKey`，三表建立，RLS 应用成功。

## 环境（一次性）
- 独立容器：`after-sales-prodtest-pg`（`postgres:17-alpine`，**全新命名卷** `prodtest-pgdata`，`initdb` 全新实例）。
- 独立网络：`prodtest-net`。
- 全新库：`after_sales_prod_clean`（`POSTGRES_DB=after_sales_prod_clean`，owner/migrator）。
- 迁移镜像：`after-sales-prod-api:latest`（**重建后** digest `sha256:5d39f030697e1b00ef83fb22a3358ddd2f923be6752a082d9f31d20f4039f293`，含修复）。
- 命令：
  ```
  docker run --rm --network prodtest-net \
    -e ENV=production \
    -e DATABASE_MIGRATOR_URL=postgresql://migrator:...@after-sales-prodtest-pg:5432/after_sales_prod_clean \
    -e APP_RUNTIME_PASSWORD=... -e BACKUP_ROLE_PASSWORD=... \
    after-sales-prod-api:latest python -m src.infrastructure.migrate_cli
  ```

## 结果
- **migrate exit 0**（stdout：`migrations applied; runtime role and backup role ensured.`）——无 `InvalidForeignKey`。

## SQL 证据（全部来自复验容器实测）
### 1) 表清单（public schema，17 张）
```
approvals, audit, checkpoint_blobs, checkpoint_migrations, checkpoint_thread_scopes,
checkpoint_writes, checkpoints, executions, memberships, operations, orders,
policy_documents, sessions, shipping_events, stream_events, streams, tenants
```

### 2) 目标三表存在
```
 orders
 policy_documents
 shipping_events
```

### 3) shipping_events 外键（已是复合外键）
```
 shipping_events_tenant_id_fkey          | FOREIGN KEY (tenant_id) REFERENCES tenants(id)
 shipping_events_tenant_id_order_id_fkey | FOREIGN KEY (tenant_id, order_id) REFERENCES orders(tenant_id, order_id) ON DELETE CASCADE
```
> 修复后：`order_id` 不再被声明为单列 `REFERENCES orders(tenant_id, order_id)`，改为表级复合外键。无 `number of referencing and referenced columns disagree`。

### 4) RLS 应用成功（enabled + FORCE + policy）
```
    table       | rls_enabled | rls_forced | policies
----------------+-------------+------------+----------
 orders           | t           | t          |        1
 policy_documents | t           | t          |        1
 shipping_events  | t           | t          |        1
```

### 5) 外键完整性实测（先插 tenants/orders，再插 shipping_events，tenant+order 匹配成功）
```
INSERT 0 1   -- tenants
INSERT 0 1   -- orders
INSERT 0 1   -- shipping_events
    k
---------
 T-A/O-1
```

## 对比：修复前同一迁移在全新库上的失败
`MIGRATE_FAILURE.log`：`sqlalchemy.exc.ProgrammingError: (psycopg.errors.InvalidForeignKey) number of referencing and referenced columns for foreign key disagree`（`order_id` 单列引用复合键）。

## 结论
- 修复有效：全新 prod-like 库 clean migrate 成功、三表建立、复合 FK 正确、RLS FORCE + 租户 policy 应用、无 InvalidForeignKey、FK 完整性 OK。
- 修复来源：`src/infrastructure/migrations.py` `shipping_events`（表级复合外键）；为**未提交工作树改动**，需随基线 commit（见 DEPLOY_BASELINE.md）。
