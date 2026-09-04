# 出网规则（Egress Policy）— 电商售后多智能体工单系统

> 目标：把 **API / Worker / 数据库 / 模型容器** 的对外网络访问收敛为一份**明确、只允许已批准
> 自托管服务地址**的允许清单；**默认拒绝外联**；**绝不把数据库/Redis 等敏感端口公开发布**；
> 模型容器（自托管 LLM）**只指向内网自托管端点**，不放开公网。
> 与本仓库《Agent 宪法》1.8（完全自托管 + 未经批准外联默认关闭）、1.7（数据面隔离）、
> 及《生产环境架构设计》保持一致。**不引入任何第三方托管 SaaS。**

---

## 1. 网络拓扑（Docker 网络分层）

| 网络 | 驱动 | internal | 挂载服务 | 作用 |
|---|---|---|---|---|
| `edge` | bridge | 否 | 仅 `nginx` | 宿主经 80/443 进出公网的**唯一通道** |
| `internal`（preview）/ 默认网络（本地 dev） | bridge | 否* | `postgres`、`redis`、`migrate`、`api`、`worker`、`frontend`、`backup` | 数据面 + 应用面私网，**不发布任何宿主端口** |
| `obs`（observability） | bridge | 是 | `clickhouse`、`minio`、`langfuse`、`langfuse-postgres`、`langfuse-redis`、`prometheus`、`grafana`、`loki`、`promtail` | 自托管可观测自网 |
| `app-net`（observability，外部网络） | external | 否 | `prometheus`、`promtail` | 复用 `after-sales-preview_internal`，用于抓取 preview `api:8000` |
| `internal`（prod） | bridge | 否* | `postgres`、`redis`、`migrate`、`api`、`worker`、`frontend`、`backup` | 生产数据面 + 应用面私网（`after-sales-prod_internal`，172.31.0.0/16），**不发布任何宿主端口** |
| `prod-net`（observability，外部网络） | external | 否 | `prometheus` | 复用 `after-sales-prod_internal`，用于抓取生产 `api:8000`（`/api/metrics`，来源命中 `METRICS_ALLOWED_SOURCES=172.31.0.0/16`） |

\* 说明：`internal: true` 在 Docker Desktop（desktop-linux/WSL2）下会破坏 Docker 内嵌 DNS 的容器名解析，
导致数据面栈起不来（已实测记录），故 `internal` 网络回退为普通 bridge。**出网硬阻断的等价落地**由下面
第 3 节的“无 host 网络 + 数据面不发布端口 + LLM/Metrics 白名单 + 网络策略清单”共同承担。

## 2. 默认出网立场

- **默认无出网**：所有业务容器都挂在受限的 Docker 私有网络（`internal`/`edge`/`obs`），**不使用
  `network_mode: host`、不使用 `host_network`**。容器默认只能访问其所在 Docker bridge 内的对等容器
  （经服务名/内网 IP）。未在下方允许清单中的目标地址一律不应可达。
- **敏感端口不公网暴露**：
  - 生产/预发布（`docker-compose.preview.yml`）：`postgres`/`redis`/**`api`/`worker`/`frontend` 均不发布任何端口**，
    仅 `nginx` 发布 80/443。
  - 本地开发（`docker-compose.yml`）：所有服务端口**仅绑定 `127.0.0.1`**（`127.0.0.1:5432`、`127.0.0.1:6379`、
    `127.0.0.1:8000`、`127.0.0.1:3000`），只对宿主机回环可达，不对外网监听。
- 若需“逐容器 IP 级出网白名单”，Docker 单机 bridge 无法原生表达，须叠加主机侧网络策略
  （如 iptables/nftables 或 Docker 网络隔离 + 反向代理唯一入口），清单见第 5 节。

## 3. 逐容器允许的出网目标（允许清单）

以下**目标地址**是各容器被允许访问的**已批准自托管服务**。除此之外一律视为未批准外联，应被阻断。

| 容器 | 允许访问的目标 | 机制 / 依据 |
|---|---|---|
| `api` | 内网 PostgreSQL（`postgres:5432`）、内网 Redis（`redis:6379`）、内网自托管 LLM 端点（`LLM_BASE_URL`，如 `model-endpoint:8001`）、内网 Langfuse（`langfuse:3000`，可选） | 共享 `internal` 网络；`LLM_ALLOWED_HOSTS` 白名单（`EndpointGuard`，受限环境空则 fail-closed）；数据面端口不发布 |
| `worker` | 内网 PostgreSQL（`postgres:5432`）、内网 Redis（`redis:6379`）、内网自托管 LLM 端点、内网 Langfuse（`langfuse:3000`，可选） | 同 `api`；共享 `internal` 网络；`execution_callback_hmac_secret` 回调链路仅向内网服务 |
| `postgres` | 无出网（仅接受 `internal` 网络内对等容器连接） | 不发布端口；无对外监听 |
| `redis` | 无出网（仅接受 `internal` 网络内对等容器连接） | 不发布端口；无对外监听 |
| `migrate` / `backup` | 内网 PostgreSQL（`postgres:5432`） | 仅一次性/定时任务；`internal` 网络 |
| `frontend` | 内网 `api:8000`（经 nginx 同源代理） | `NEXT_PUBLIC_API_BASE_URL=/api`；`internal` 网络 |
| 自托管 LLM / 模型容器 | 无公网；仅接受 `internal`/受控网络内的 `api`/`worker` 调用 | `LLM_BASE_URL` 保持内网（如 `http://model-endpoint:8001/v1`）；`LLM_ALLOWED_HOSTS` 只含内网 host/CIDR；不放开公网 |
| 可观测容器（`prometheus`/`promtail`/`grafana`/`loki`/`langfuse-*`） | 内网 `api:8000`（Prometheus 抓取）、内网对等组件 | `obs`（`internal:true`）自网 + `app-net`/`internal` 仅用于抓取指标与跟随应用日志 |

### LLM / 模型容器出网红线
- `LLM_BASE_URL` 必须指向**内网自托管**端点；`LLM_ALLOWED_HOSTS` 必须显式列出该内网主机/CIDR。
- 受限环境（preview/production）下 `LLM_ALLOWED_HOSTS` 为空 → `EndpointGuard` **fail-closed**：
  拒绝访问任何端点，阻断未批准外联（见 `src/llm/security.py`）。
- **绝不**把模型调用指向第三方托管 SaaS；也**不**为模型容器打开公网出口。

## 4. Prometheus 指标抓取路径与来源限制

- **抓取路径**：自托管 Prometheus **不经过公网 nginx**，而是经 `app-net`（外部网络，复用
  `after-sales-preview_internal`）与 `prod-net`（外部网络，复用 `after-sales-prod_internal`）
  **直连两栈的 `api:8000/api/metrics`**（见 `docker-compose.observability.yml`）。两栈各自存在名为 `api`
  的服务，为避免 DNS 歧义，`docker-compose.observability.yml` 用**容器名**把预览/生产 API 分开：
  - `job_name: api` → `after-sales-preview-api-1:8000`（preview，经 `app-net`）；
  - `job_name: api_prod` → `after-sales-prod-api-1:8000`（prod，经 `prod-net`）。
- **公网阻断**：`deploy/nginx/preview.conf`（及生产 `prod.conf`）在 443 server 增加精确匹配
  `location = /api/metrics { return 404; }`——公网经 nginx 访问 `/api/metrics` 一律 404，不转发到 `api:8000`。
- **应用层来源白名单（最终兜底）**：`/api/metrics` 只允许来源 IP 命中 `METRICS_ALLOWED_SOURCES`
  （IP/CIDR 列表）才返回指标，否则 `403`。
  - 受限环境（preview/production）恒为“仅内网”：`METRICS_EXPOSE_INTERNAL_ONLY=true`，
    且 `METRICS_ALLOWED_SOURCES` 与对应 `networks.internal.ipam.subnet` 对齐（preview 默认 `172.30.0.0/16`、
    **prod 为 `172.31.0.0/16`**——见 `docker-compose.prod.yml`，与 `after-sales-prod_internal` 实际子网一致）。
  - 来源 IP 判定复用 `LOGIN_TRUSTED_PROXY_DEPTH` 的**可信代理深度**：从 `X-Forwarded-For` 取最后一个
    可信值（可信 nginx 之后）；无代理（直接内网连接，如 Prometheus 直连）时取直连 peer。
    **可信边界**：仅当 nginx 为唯一可信反向代理时该头才可信；若服务被直接挂到公网，
    必须设 `LOGIN_TRUSTED_PROXY_DEPTH=0`（不信任任何 `X-Forwarded-For`）。
  - 指标只暴露**有界聚合**（`route`/`status` 等维度），绝不含 `tenant_id` 等租户明细（见 `metrics.py`）。

## 5. 主机侧网络策略清单（若要达到“逐容器 IP 白名单”）

单机 Docker bridge 无法把“出网到某 IP”做成逐容器白名单；要达到企业级出网白名单，建议（选择性）：
1. **仅保留 nginx 作为唯一公网入口**（`edge` 网络），其余容器无 `host` 网络、无任意发布端口。
2. 主机防火墙（iptables/nftables）对 Docker 网桥：仅放行 80/443（宿主机 → `nginx`），
   放行 `internal` 网段内容器间通联，**默认 DROP** 容器网段 → 公网/其它网段的外联。
3. 对“容器 → 公网”的 NAT 出站，仅放行到 `METRICS_ALLOWED_SOURCES` / `LLM_ALLOWED_HOSTS` 中的
   内网地址（等同第 3 节允许清单），其余一律拒绝。
4. 完全自托管组件的遥测/自动更新默认关闭（Langfuse 已设 `TELEMETRY_ENABLED=false`；
   模型/LLM 端点无公网出口）；如需外联必须走受控网络策略审批并留痕。

## 6. 部署生效要点（必读）

- **重建 `internal` 网络**：本策略给 `networks.internal` 固定子网 `172.30.0.0/16`。若该网络已以其它
  子网被创建（旧栈），`docker compose up` 会报子网冲突，需先 `docker compose -f docker-compose.preview.yml down`
  并 `docker network rm after-sales-preview_internal`（连同 `after-sales-observability` 的 `app-net`
  外部引用一并重建），再按顺序先起 **preview 栈**、后起 **observability 栈**。
- **密钥与必需环境变量**：按 `deploy/.env.preview` 注入 `POSTGRES_PASSWORD`、`REDIS_PASSWORD`、
  `APP_RUNTIME_PASSWORD`、`AUTH_JWT_SECRET`、`AUTH_LOGIN_CREDENTIALS`、`LLM_API_KEY`、
  `LLM_ALLOWED_HOSTS`、`EXECUTION_CALLBACK_HMAC_SECRET`、`LAUNCH_ALLOWED_TENANTS` 等；
  `METRICS_ALLOWED_SOURCES` 默认 `172.30.0.0/16`，如需覆盖（如改用其它内网网段）可在 `.env.preview` 覆盖。
- **验证**：
  - `curl -k https://<public>/api/metrics` → 期望 `404`（公网 nginx 阻断）；
  - 在内网（observability 的 Prometheus 容器）`curl http://api:8000/api/metrics` → 期望 `200`
    （来源命中白名单）；
  - `docker compose config` 通过（env 注入后），`nginx -t` 通过（preview.conf）。
