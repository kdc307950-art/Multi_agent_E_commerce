# 沙箱网关 Compose 化 + 沙箱闭环验证（Captain 交付 · t4）

> 角色：captain · 范围：计划阶段三「沙箱执行、回调和故障演练」之「将 sandbox_http 作为 Compose 服务部署」
> 日期：2026-09-05 · 结论：**配置层面已落地 + 沙箱闭环本地验证通过**；容器级启动受本机 Docker 引擎不可用限制（见 4）。

---

## 1. 落地内容（仓库可追溯）

### 1.1 `docker-compose.preview.yml`
- 新增 `sandbox-gateway` 服务：复用根 `Dockerfile`，`command: ["python", "-m", "src.execution.sandbox_gateway"]`，监听 `8010`，沙箱 SQLite 持久化到 `./data/preview-sandbox`，只挂 `internal` 私有网（**不发布任何宿主端口**），`SANDBOX_GATEWAY_API_KEY` 由服务器环境密钥注入（缺失 fail-closed）。
- `api` / `worker` 环境：`EXECUTION_PROVIDER` 默认改为 `sandbox_http`，新增 `GATEWAY_BASE_URL=http://sandbox-gateway:8010`、`GATEWAY_API_KEY`、`GATEWAY_TIMEOUT_SECONDS`。
- `api` / `worker` 的 `depends_on` 增加 `sandbox-gateway: condition: service_healthy`。

### 1.2 `docker-compose.prod.yml`
- 同 preview：新增 `sandbox-gateway` 服务（`image: after-sales-prod-api`），api/worker 接线 `GATEWAY_BASE_URL` 并默认 `EXECUTION_PROVIDER=sandbox_http`，depends_on 增加 sandbox-gateway。

### 1.3 安全默认（遵守宪法）
- `EXECUTION_MODE` 恒为 `shadow`（沙箱），**不切 live**、不触真实资金。
- sandbox-gateway 只挂 `internal` 私有网，不发布宿主端口。
- `GATEWAY_API_KEY` / `SANDBOX_GATEWAY_API_KEY` 由服务器环境密钥注入，绝不硬编码明文。

---

## 2. 本地验证证据

### 2.1 compose 配置解析
`docker compose -f docker-compose.preview.yml config --services`（填充必需密钥后）返回服务列表，`sandbox-gateway` 已出现：
```
postgres  migrate  redis  sandbox-gateway  api  frontend  nginx  backup  worker
```
`docker-compose.prod.yml` 同理，`sandbox-gateway` 已出现。**（引擎不可用仅影响启动，不影响配置解析。同类命令在引擎可用机器的等效 run 见验证记录。）**

### 2.2 sandbox 闭环（provider ↔ gateway 真实 HTTP）
用 `sandbox_gateway` 独立进程（`python -m src.execution.sandbox_gateway`，端口 8010）+ `SandboxHttpFundsProvider(api_key="test-key")` 实测：
- `submit` → `query` 链路返回一致 `external_txn_id`；
- **幂等**：同租户同 `idempotency_key` 重复 submit 返回**同一** `external_txn_id`（`txn-3763ef0296054224`）；
- **跨租户隔离**：不同租户同 `idempotency_key` 生成**不同** `external_txn_id`（`txn-60c43c83ed2c45d4`）。

> 幂等闭环与租户级幂等隔离均验证通过（对应计划阶段三「验证幂等」与「跨租户回调/幂等不覆盖」）。

---

## 3. 与既有实现的衔接

- `src/execution/sandbox_gateway.py` 早已实现（SQLite 持久化幂等 + submit/query/compensate + 故障注入接缝）；本任务补齐了此前缺失的 **Compose 服务定义与接线**。
- `src/execution/provider.py::SandboxHttpFundsProvider` + `build_provider(settings)` 对 `EXECUTION_PROVIDER=sandbox_http` 的 fail-closed（缺 `gateway_base_url` 抛 `gateway_unconfigured`）已存在且符合宪法。
- `deploy/.env.preview.example` 此前已配置 `EXECUTION_PROVIDER=sandbox_http` + `GATEWAY_BASE_URL=http://sandbox-gateway:8010`，与本次 compose 落地对齐。

---

## 4. 诚实边界（未执行 / 需运行态）

- **容器级启动/镜像重建**：本机 Docker 引擎不可连接（`npipe:////./pipe/dockerDesktopLinuxEngine` 缺失），故「compose up 后 sandbox-gateway 容器健康」与「api 经 GATEWAY 真实触发沙箱执行」的**运行态验证为部署期复验项**，需在能连 Docker 引擎（且能 seed 真实租户/注入密钥）的目标机器上执行。本落地在配置解析 + 独立进程闭环层面已验证。
- 沙箱不自触真实资金：仅对 `shadow` 模式（不切 live）生效；真实资金链路/G6 仍需外部资金渠道 + 批准。
