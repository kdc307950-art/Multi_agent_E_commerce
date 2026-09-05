# 沙箱网关 Docker 运行态验证（deploy-engineer · 阶段三）

> 角色：deploy-engineer（沙箱网关 / Docker Compose 运行验证工程师）· rc4-productionization · 任务 t3
> 环境：**Docker Engine 29.7.2（Docker Desktop 已连接）**；Docker Compose v5.5.0；镜像 `after-sales-prod-api:latest`（已构建，`--no-build` 复用，未重建）。
> 结论先行：**`sandbox-gateway` 已在真实 Docker 运行态拉起并全链路通过**；执行层「不重复执行 / 不跨租户 / 不丢状态」在运行态用 4 个维度实测闭环。**未触真实资金。**

---

## 0. 运行态实测总览（全部 `PASS`）

| 验证维度 | 结果 | 证据 |
|---|---|---|
| 网关容器可启动、健康检查通过 | ✅ 运行态实测 | `Up (healthy)`；healthcheck `socket.create_connection(127.0.0.1:8010)` 通过 |
| 网关仅挂 internal 私有网、无宿主端口暴露 | ✅ 运行态实测 | `PortBindings={}`、`Ports={"8000/tcp":null}`、仅 `after-sales-verify_verify-net` |
| 另一容器（同 internal 网）能经服务名访问网关 | ✅ 运行态实测 | verify-runner 经 `http://sandbox-gateway:8010` 提交/查询全部 200：网关访问日志 `172.25.0.3 → POST /api/sandbox/submit 200` |
| SQLite 数据卷持久化（网关重启后仍幂等） | ✅ 运行态实测 | `docker restart` 网关后重跑，同键返回**同一** `external_txn_id=txn-b6f6f7ed4f7c4ae8`；卷文件 `data/verify-sandbox/sandbox_gateway.db` |
| 幂等（同租户同键、跨租户隔离、并发同键、服务端单行） | ✅ 运行态实测 | 见 `IDEMPOTENCY_RUNTIME.md` |
| 回调安全（验签/重放/终态封闭/未知/跨租户/篡改金额） | ✅ 运行态实测 | 见 `CALLBACK_SECURITY_RUNTIME.md` |
| 故障注入（5xx/超时/网关down） | ✅ 运行态实测 | 见 `FAULT_INJECTION_RUNTIME.md` |
| 对账（overdue 收敛 / 缺失转人工） | ✅ 运行态实测 | 见 `RECONCILIATION_RUNTIME.md` |

---

## 1. 编排/健康检查/网络

**拉起命令（仓库根目录；独立 project `after-sales-verify`，不触碰 preview/prod 数据卷）**
```
docker compose -f compose.verify.yml up -d --no-build
docker compose -f compose.verify.yml config --quiet    # EXIT=0 通过
docker compose -f compose.verify.yml ps
```

**健康检查采样**：`[sandbox-gateway] Up 15 seconds (healthy)`。

**无宿主端口暴露（关键红线）**
```
docker port after-sales-verify-sandbox-gateway-1          # 无输出 → 未发布端口
docker inspect -f '{{json .HostConfig.PortBindings}}' ...  # {}
docker inspect -f Networks={{...}} | Ports={{json .NetworkSettings.Ports}}
   → Networks=after-sales-verify_verify-net | Ports={"8000/tcp":null}
```
`8000/tcp` 仅为镜像 `EXPOSE 8000`（API 端口），宿主无映射；网关实际监听容器内 8010。

**内网可达（另一容器按服务名访问）**：网关日志
```
INFO: 172.25.0.3:47540 - "POST /api/sandbox/submit HTTP/1.1" 200 OK
INFO: 172.25.0.3:47540 - "GET /api/sandbox/query?tenant_id=TENANT-A&external_txn_id=... HTTP/1.1" 200 OK
```
172.25.0.3 = verify-runner 容器（与网关同网），即**与 api/worker 同位的任意容器都能经 `sandbox-gateway:8010` 服务名通信**。

---

## 2. 数据卷持久化（不丢状态）

`/data` = host 绑定挂载 `./data/verify-sandbox`（与 preview/prod 完全隔离）。
```
Get-ChildItem data/verify-sandbox → sandbox_gateway.db (24576 B) + -shm/-wal
```
**重启前后对比**：
| 阶段 | `rt-key-aaa` 的 external_txn_id |
|---|---|
| 首次提交 | `txn-b6f6f7ed4f7c4ae8` |
| `docker restart` 网关后重跑 | **`txn-b6f6f7ed4f7c4ae8`（同一值）** |

→ 网关容器重启后，SQLite 持久化的幂等表仍在，同键返回同一外部队列号：**不丢状态 + 跨进程幂等**。

---

## 3. 对「master report §5 声称 prod compose 缺 sandbox-gateway」的修正

**精确事实**（纠正/细化 master report `RC3_PROD_READINESS_MASTER_REPORT.md` §四.5 与 §三.5）：

| 项 | master report 声称 | 实际（本次实测） |
|---|---|---|
| prod compose 有无 `sandbox-gateway` | 「无该服务」 | ✅ **有**：`docker-compose.prod.yml` L75-94 定义 `sandbox-gateway` |
| prod 有无 `GATEWAY_BASE_URL` | 「无」 | ✅ **有**（默认 `http://sandbox-gateway:8010`，L145）；`GATEWAY_API_KEY` L146 为必填 |
| `EXECUTION_PROVIDER` | 「=mock」 | ⚠️ **`docker-compose.prod.yml` 默认 `sandbox_http`（L143）**，但 **`deploy/.env.production` 显式 `EXECUTION_PROVIDER=mock`（覆盖默认）** |
| 生产栈能否直接拉起 | （隐含否） | ❌ **当前不能**：`docker compose --env-file deploy/.env.production config --quiet` 因 **`GATEWAY_API_KEY` / `SANDBOX_GATEWAY_API_KEY` 缺失** 而 **interpolation FAIL（exit 1）** |

**结论**：compose 文件**已经**接上 `sandbox-gateway`（代码侧就绪），真正缺口在**部署环境**——`deploy/.env.production` 未注入 `GATEWAY_API_KEY` / `SANDBOX_GATEWAY_API_KEY`，且 `EXECUTION_PROVIDER=mock`。因此按现状 `docker compose up` 生产栈会在 config 阶段失败（fail-closed，符合宪法「缺配置即拒绝」）。**这是待部署项的配置缺口（BLOCKED-需注入网关密钥 + 切换 provider），不是代码缺失。**

---

## 4. 待部署 / 本环境无法闭环项（诚实标注）

- **api/worker 完整起栈后直连网关**：本次未全栈拉起 api/worker（生产栈因缺网关密钥 config 失败；preview/prod 历史容器曾 OOM Exit 137）。网关「内网可达」已由同网 verify-runner **运行态实测**（即 api/worker 同机制），但完整 api/worker 启动到 healthy 属**部署期执行项**。
- **真实外部渠道 / 真实资金**：始终不触。网关为完全自托管沙箱（SQLite 幂等），非真实支付/电商网关。
- **PG/Redis 重启、Celery 重复投递、checkpoint 恢复失败、审批超时、回调丢失**：这些依赖 postgres/celery/checkpointer 组件的故障由其他角色（test-runner/security-auditor）在 PG 端验证；本任务聚焦**网关 + 执行层**的确定性收敛（5xx/超时/网关down 已实测，幂等结算/回调重放/对账 mismatch 已实测）。

---

## 5. 清理与回收

验证结束后已 `docker compose -f compose.verify.yml down` 移除容器 + 网络；`data/verify-sandbox`、`compose.verify.yml`、`_verify_runtime/` 为**临时可回收**验证工件（脚本/副本保留在证据目录以复现）。
