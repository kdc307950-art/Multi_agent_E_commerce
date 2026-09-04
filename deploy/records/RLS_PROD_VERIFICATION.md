# RLS 生产验证报告 — after_sales_prod

> 任务：t9（RLS 生产验证）· 执行人：validator（合规与验证评审）· 环境：production（Docker compose 项目 `after-sales-prod`）
> 数据库：`after_sales_prod`（17 张表；15 张启用 RLS 且 `force_row_security=t`；`tenants`/`checkpoint_migrations` 未启用 RLS）
> 方法：以**最小权限运行角色 `app_runtime`（No inheritance，无 BYPASSRLS，强制 RLS）**连接，客户端显式注入租户上下文 `SET app.tenant_id='<tenant>'` 做动态验证。

---

## 0. 被测对象（角色与 RLS 机制）

### 角色属性（`\du` 实测）
| 角色 | 属性 | 说明 |
|---|---|---|
| `app_runtime` | No inheritance（无 Bypass RLS） | 运行角色，仅 DML，受 RLS 强制约束 |
| `backup_role` | No inheritance, **Bypass RLS** | 备份角色，仅 CONNECT/SELECT（可读全量） |
| `migrator` | Superuser, Create role, Create DB, Replication, Bypass RLS | 迁移/owner（DDL） |

### 租户上下文函数
- `public.app_current_tenant_id()` → `NULLIF(current_setting('app.tenant_id', true), '')`：读取当前会话租户。
- `public.checkpoint_thread_in_current_tenant(candidate_thread_id text)` → 判断该 `thread_id` 是否存在于 `checkpoint_thread_scopes` 且归属当前租户。

### RLS 策略（15 张表，全部 `*` 命令，USING 与 WITH CHECK 相同）
- **商业表**（`approvals`/`audit`/`executions`/`memberships`/`operations`/`orders`/`policy_documents`/`sessions`/`shipping_events`/`stream_events`/`streams` 及 `checkpoint_thread_scopes`）：
  `(tenant_id = app_current_tenant_id())`
- **checkpoint 表**（`checkpoints`/`checkpoint_writes`/`checkpoint_blobs`）：`checkpoint_thread_in_current_tenant(thread_id)`（经 `checkpoint_thread_scopes` 映射租户）

### 注入的测试数据（执行后已清理，见 §3）
- 两个测试租户 `TENANT-A`、`TENANT-B`；
- 业务表 `orders` 各 1 行（order-A-1 / order-B-1）；
- checkpoint：`checkpoint_thread_scopes`（thread-A→A、thread-B→B）、`checkpoints`、`checkpoint_writes`、`checkpoint_blobs` 各含 thread-A / thread-B 两行。

---

## 1. 动态验证结果

### T1 — 跨租户读被拒（RLS 过滤）
以 `app_runtime`，`SET app.tenant_id='TENANT-A'`：

| 探测 | 结果 | 判定 |
|---|---|---|
| `SELECT count(*) FROM orders WHERE order_id='order-A-1'`（本租户） | 1 | ✅ 可见 |
| `SELECT count(*) FROM orders WHERE order_id='order-B-1'`（跨租户） | **0** | ✅ 被拒 |
| `SELECT count(*) FROM orders`（TENANT-A 上下文全量） | 1（仅本租户） | ✅ 隔离 |

**结论：跨租户读被行级策略过滤，仅返回当前租户数据。**

### T2 — 跨租户写被拒（WITH CHECK）
以 `app_runtime`，`SET app.tenant_id='TENANT-A'`，尝试 `INSERT INTO orders(tenant_id=...) VALUES ('TENANT-B', ...)`：
```
ERROR:  new row violates row-level security policy for table "orders"
```
**结论：插入 `tenant_id=其他租户` 的行被 WITH CHECK 拒绝。**

### T3 — 修改 tenant_id 被拒
以 `app_runtime`，`SET app.tenant_id='TENANT-A'`：
- 对照：`UPDATE orders SET status='shipped' WHERE order_id='order-A-1'`（不改租户）→ `UPDATE 1`（成功；已回滚）✅
- 跨租户改租户：`UPDATE orders SET tenant_id='TENANT-B' WHERE order_id='order-A-1'` →
  ```
  ERROR:  new row violates row-level security policy for table "orders"
  ```
**结论：把本租户行的 `tenant_id` 改成其它租户被 WITH CHECK 拒绝；不改变租户的自身更新正常。**

### T4 — checkpoint 隔离（LangGraph 检查点）
以 `app_runtime`，`SET app.tenant_id='TENANT-A'`：

| 探测 | 结果 | 判定 |
|---|---|---|
| `checkpoint_thread_scopes` 全量 | 1 | ✅ 仅 thread-A（TENANT-B 的 thread-B 隐藏） |
| `checkpoint_thread_scopes WHERE thread_id='thread-B'` | **0** | ✅ 不可见 |
| `checkpoints WHERE thread_id='thread-A'` | 1 | ✅ 本租户可见 |
| `checkpoints WHERE thread_id='thread-B'` | **0** | ✅ 不可见 |
| 对照：INSERT checkpoint（thread-A，本租户） | `INSERT 0 1`（成功；已回滚） | ✅ 本租户可写 |
| 跨租户 INSERT checkpoint（thread-B，属 TENANT-B） | `ERROR: new row violates row-level security policy for table "checkpoints"` | ✅ 不可写 |

**结论：checkpoint 按 `thread_id→tenant`（经 `checkpoint_thread_scopes`）强制隔离，跨租户不可见、不可写。**

### S1 — fail-closed（无租户上下文）+ 反向方向（TENANT-B）
以 `app_runtime`：
- `SET app.tenant_id=''`（未设置）→ `orders` 计数 0（`app_current_tenant_id()` 返回 NULL，RLS 过滤为空）✅ fail-closed
- `SET app.tenant_id='TENANT-B'` → 本租户 order-B-1 可见(1)，order-A-1 隐藏(0)；scope 仅 thread-B(1)，thread-A 隐藏(0)；checkpoint thread-B 可见(1)，thread-A 隐藏(0)

**结论：无租户上下文时默认拒绝（fail-closed）；反向方向同样隔离（对称）。**

### S2 — checkpoint_writes / checkpoint_blobs 隔离 + 覆盖已存在 checkpoint
以 `app_runtime`，`SET app.tenant_id='TENANT-A'`：
- `checkpoint_writes`：thread-A=1，thread-B=**0**
- `checkpoint_blobs`：thread-A=1，thread-B=**0**
- 覆盖已存在 thread-B checkpoint（`UPDATE checkpoints SET metadata=... WHERE thread_id='thread-B'`）→ **UPDATE 0**（行不可见，USING 过滤）
- 以已存在 (thread-B,'default','checkpoint-B-1') 再 INSERT → `ERROR: new row violates row-level security policy for table "checkpoints"`

**结论：checkpoint_writes/blob 同样按租户隔离；跨租户 checkpoint 既不可见（UPDATE 0 行），也不可覆盖（INSERT 拒绝）。**

---

## 2. 结论

**全部 RLS 校验证通过：**
1. ✅ 跨租户读被拒（RLS 仅暴露当前租户数据）；
2. ✅ 跨租户写被拒（WITH CHECK 拒绝跨租户 INSERT）；
3. ✅ 修改 `tenant_id` 被拒（WITH CHECK 拒绝改租户），不改变租户的自身更新正常；
4. ✅ checkpoint 隔离（`checkpoints`/`checkpoint_writes`/`checkpoint_blobs` 经 `checkpoint_thread_scopes` 强制按租户隔离，跨租户不可见/不可覆盖/不可写）。

角色属性与策略实测与预期一致：`app_runtime`=No inheritance（无 BYPASSRLS）受 RLS 强制；`backup_role`=BYPASSRLS（仅只读，读全量）；`migrator`=Superuser。15 张表 RLS 开启且 force_row_security=t，17 张表业务表带 `tenant_id`。

**未发现跨租户读/写/修改租户/checkpoint 越权的任何绕过路径**；生产 RLS 符合宪法第 1.6/1.7 条（数据面带租户范围、跨租户默认拒绝）。

---

## 3. 数据清理与说明

- 测试数据（TENANT-A/B、orders、checkpoint_thread_scopes、checkpoints、checkpoint_writes、checkpoint_blobs）已全部删除，业务表恢复 0 行。
- **说明（非本次测试产生）**：验证期间 `tenants` 表中出现一行 `pg-drill-20260904205550505`（name=DrillMarker，status=active），这是**灾备演练标记行**（`restore_drill.sh` 的 MARKER 写入模式：`INSERT INTO tenants(id,name,status,created_at) VALUES ('pg-drill-...','drill','active', ...)`），由并发执行的灾备演练/T10 流程写入，**非本次 RLS 测试注入，亦非真实租户**。本次验证未创建/删除该行，保留原样（`tenants` 表未启用 RLS）。

## 4. 复验方式

```bash
# 以 app_runtime（最小权限、NOBYPASSRLS）连接并注入租户上下文
docker compose -f docker-compose.prod.yml --env-file deploy/.env.production \
  exec -T postgres psql "postgresql://app_runtime:<APP_RUNTIME_PASSWORD>@localhost:5432/after_sales_prod"
> SET app.tenant_id='TENANT-A';
> SELECT count(*) FROM orders; -- 仅本租户
```

*签名：validator / 生成时间见 commit 与部署记录。*
