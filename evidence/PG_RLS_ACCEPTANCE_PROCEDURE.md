# PostgreSQL / RLS 业务数据源验收流程（可重复）

> **目的**：验证"业务读路径接入受控真实系统"在 PostgreSQL/RLS 数据面上真正生效——同租户可读、
> 跨租户零可见、未设置租户作用域时零可见；Mock 仅保留在测试环境。
> **当前状态**：本会话环境**无 Docker/PostgreSQL**（`docker` 缺失、5432 未监听），故以下为**可重复验收流程**，
> 在具备 PostgreSQL 的环境（本机 `docker compose up -d` 或租赁 IaaS）执行；未执行前**不得宣称生产可用**。

---

## 0. 结论摘要（供验收记录）

| 验收项 | 断言 | 依据 |
|---|---|---|
| 订单/物流/政策读路径经受控数据源 | 业务节点/读路由经 `EcommerceAdapter` + `BusinessDataSource`，不再直接读 `mock_data` | `tests/test_data_source_contract.py`、`tests/test_security_regressions.py` |
| 服务端注入 `tenant_id` | 数据源查询一律以 `set_config('app.tenant_id', :t, true)` 在事务内注入 | `src/tools/postgres_data_source.py` |
| 跨租户订单/物流读取被拒 | 同租户命中；跨租户返回 None；RLS 强制零可见 | `test_data_source_contract.py::test_pg_data_source_*`（`@pytest.mark.postgres`） |
| RLS 兜底 | 未经租户过滤的查询也因 FORCE RLS 零可见 | `test_pg_data_source_rls_filters_unauthorized_rows` |
| Mock 仅测试环境 | 受限环境（preview/production）`business_data_backend` 必须 postgres，否则门控标记 | `test_launch_gate_audit.py::test_launch_gate_flags_restricted_env_with_mock_business_data_source` |

---

## 1. 前置条件（需要 PostgreSQL）

```bash
# 方式 A：项目自托管 compose（会启动 postgres:17-alpine 及依赖）
docker compose up -d postgres

# 方式 B：任意自管 PostgreSQL，把连接串导出
export DATABASE_URL="postgresql://<migrator_user>:<password>@127.0.0.1:5432/aftersales"
```

必须使用**迁移/表 owner** 角色执行 `initialize_all`，随后创建**运行角色 `app_runtime`**
（`LOGIN NOINHERIT NOSUPERUSER NOBYPASSRLS`，仅供 DML，绝不作为表 owner）。

---

## 2. 步骤

1. **应用迁移**（建业务表 + RLS + 函数；幂等）：
   ```python
   from sqlalchemy import create_engine
   from src.infrastructure import migrations
   from src.infrastructure.postgres_store import _to_sqlalchemy_url
   eng = create_engine(_to_sqlalchemy_url(DATABASE_URL), pool_pre_ping=True)
   migrations.initialize_all(eng)          # tenants/orders/shipping_events/policy_documents ... + RLS
   migrations.apply_runtime_role(eng, password=os.environ["APP_RUNTIME_PASSWORD"])  # 可选：建议
   ```

2. **预置两租户业务数据**（示例见 `tests/test_data_source_contract.py::_seed_pg_business`）：
   在 `tenants` 写入 TENANT-A、TENANT-B；在 `orders` 写 订单 `ORD-A1`（tenant TENANT-A）；
   `shipping_events` 写 TENANT-A 轨迹；`policy_documents` 写 TENANT-A / TENANT-B 政策各一。

3. **运行运行角色连接 + RLS 验收**：执行 `@pytest.mark.postgres` 用例：
   ```bash
   # 用连接串（运行角色 app_runtime）跑业务数据面 + 数据面 RLS 验收（无 DB 时整组 skip）
   DATABASE_URL=... APP_RUNTIME_PASSWORD=... \
     python -m pytest tests/test_pg_data_source_acceptance.py tests/test_data_source_contract.py tests/test_pg_rls.py -m postgres -q
   ```

---

## 3. 预期输出（在无超管绕过的前提下）

- `tests/test_pg_data_source_acceptance.py`（`@pytest.mark.postgres`，以运行角色 `app_runtime` 连接）：
  - `test_pg_business_ds_acceptance_same_tenant_readable`：PASS——`get_order`/`get_shipping`/`search_policy`
    在本租户作用域内命中（订单 `ORD-A1`/轨迹/政策均可见）。
  - `test_pg_business_ds_acceptance_cross_tenant_zero_visible`：PASS——跨租户读同订单号返回 None；
    政策文档仅本租户命中。
  - `test_pg_business_ds_acceptance_unset_tenant_zero_visible`：PASS——未设置 `app.tenant_id` 时
    `orders`/`shipping_events`/`policy_documents` 三表零可见。
- `tests/test_data_source_contract.py`：
  - `test_pg_data_source_tenant_isolation_and_rls`：PASS——`get_order("TENANT-A","ORD-A1")` 命中；
    `get_order("TENANT-B","ORD-A1") is None`；`get_shipping` 同断言；`search_policy` 仅命中本租户。
  - `test_pg_data_source_rls_filters_unauthorized_rows`：PASS——即使 SQL 不带租户过滤，
    由于 `FORCE RLS + set_config('app.tenant_id')`，跨租户文档零可见。
- `tests/test_pg_rls.py`：PASS——会话/审批/检查点数据面的跨租户零可见、零可写、无作用域零可见、
  FORCE RLS 对 owner 绕过也过滤、官方 checkpoint 表经 `checkpoint_thread_scopes` 存在性关联。
- 若无 `DATABASE_URL` / 连接失败：以上用例整组 **skip**（本会话因此 skip，不代表通过）。

---

## 4. 对应源码落点

| 组件 | 文件 |
|---|---|
| 业务读路径契约 | `src/tools/data_source.py`（`BusinessDataSource`/`MockBusinessDataSource`/`build_data_source`） |
| 真实数据源（RLS） | `src/tools/postgres_data_source.py` |
| 订单/物流/政策表 + RLS 迁移 | `src/infrastructure/migrations.py`（`orders`/`shipping_events`/`policy_documents` + `_tenant_scope` policy） |
| 适配器（租户/归属/资格） | `src/tools/adapter.py` |
| 读路由（经 adapter，禁直接读 mock） | `src/api/routes.py`（`GET /api/orders/{order_id}`） |
| Mock 仅测试环境门控 | `src/core/launch_gate.py`；`src/config.py`（`business_data_backend`） |
| skip-if-no-PG 断言（业务数据面验收） | `tests/test_pg_data_source_acceptance.py`（同租户可读/跨租户零可见/未设置作用域零可见） |
| skip-if-no-PG 断言（数据面 RLS） | `tests/test_data_source_contract.py`（`@pytest.mark.postgres`）；`tests/test_pg_rls.py` |

---

## 5. 待真正运行的前提（诚实标注）

执行本流程并得到上述 PASS 需要：**一台可用的 PostgreSQL**（Docker 或租赁 IaaS 自管）+ 可迁移角色 +
已创建的运行角色 `app_runtime`（NOBYPASSRLS）。本会话环境不具备，故该项为"**待基础设施**"，
在具备环境后执行第 2 节即可获得可复核证据，不得以"代码就绪"代替真实运行结果。
