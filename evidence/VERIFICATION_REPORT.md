# 电商售后多智能体工单系统 — 本阶段验收与证据报告

> 版本：v1.1 · 2026-09-03 · 工作区提交 `561e97e`（main）
> 本报告为**本阶段交付**的验收证据；请与《生产基线与验收测试》《前端体验与工作台设计》配套阅读。
> 纪律：本地/Mock 已验证 与 待预发布/Docker 验证 严格分开；凡是未在真实环境运过的一律标“未验证”。
> 更新（v1.1）：已在本机用运行中的 **PostgreSQL 17.11 + Redis 6.0.16**（Compose 栈，`localhost:5432`/`6379` 可达）补齐 **PostgreSQL 数据面集成测试**，消除此前“Postgres/RLS 未验证”边界。

---

## 一、交付内容与验收对照

| 本阶段要求 | 交付物 | 状态 | 证据 |
|---|---|---|---|
| ① 客户会话、会话历史、订单查询、审批状态展示、客服工作台 | `frontend/app/(app)/chat`、`frontend/app/(app)/sessions`、`frontend/components/ContextPanel`（订单/物流查询 + 审批状态）、`frontend/app/(app)/workbench`（三栏工作台） | 已实现并经真实浏览器验证 | `evidence/screenshots/02_chat_customer.png`、`03_customer_approval_required.png`、`07_workbench_admin.png`、`08_sessions_admin.png` |
| ② 按 customer/agent/admin/approver 控制菜单与数据可见范围 | `frontend/lib/auth.ts`（角色菜单 ROLE_MENUS/ROLE_HOME）；后端 RBAC：`list_sessions`/`approvals`/`operation` 按角色过滤、`/api/members` 仅 admin | 已实现；后端强制 + 前端按角色渲染 | 截图 `01-11`；后端 `GET /api/members` 非 admin 403（`tests/test_e2e_flow.py::test_role_based_data_visibility`） |
| ③ SSE 断线恢复、事件去重、错误与人工升级展示 | `frontend/lib/sse.ts`（SseChannel：按事件 id 去重、resume 断线恢复、状态机）；`frontend/lib/useSseChat.ts`（错误/人工升级/审批提示）；后端 `POST /api/chat` mode=resume + Last-Event-ID | 已实现并通过 API + 浏览器验证 | 后端 `run_resume` 只重放不重跑；`tests/test_chat_sse.py::test_resume_requires_last_event_id` |
| ④ 审批中心二次确认、状态刷新、operation 查询 | `frontend/app/(app)/approvals`（二次确认弹窗、刷新状态、操作状态查询）；后端 `POST /api/approvals/{id}/decision` 强制 `confirmation` + 绑定校验 + CAS | 已实现并经真实浏览器验证 | `evidence/screenshots/05_approvals_confirm_modal.png`、`06_approvals_approved.png`；`tests/test_approval_idempotency.py` |
| ⑤ 真实浏览器验证前端与 API 的跨域/反向代理链路 | Next.js `next.config.mjs` rewrites（`/api/:path*` → 后端）；浏览器 → `:3100` 同源 `/api` → 后端 `:8000`；后端 `settings.enable_cors` 可选白名单 | 已用真实浏览器（Playwright + 系统 Chrome）验证完整流程 | `browser_e2e.mjs` → `BROWSER_E2E_OK`；`evidence/proxy_e2e.json`；截图 `b03/b05/b06` |
| ⑥ Compose、数据库恢复、Redis 重启、worker 重启、并发测试 | Compose 栈由外部编排运行（`:3000` 容器）；数据库恢复与并发在应用层做等价验证（SQLite online backup API + CAS 并发）；Redis/worker 重启未在实机执行 | **部分完成/部分未验证**（见§五） | `tests/test_recovery.py`、`tests/test_sqlite_store.py::test_sqlite_concurrent_claim_single_winner`；`evidence/recovery.json` |
| ⑦ 记录版本、日志、截图、测试数、RPO/RTO、未验证边界 | 本报告 + `evidence/`（JSON 证据 + screenshots/）+ 后端 uvicorn 日志 | 已产出 | 见§二~§五 |

---

## 二、版本清单

| 组件 | 版本/标识 |
|---|---|
| Python | 3.12.9 |
| FastAPI | 0.141.1 |
| LangGraph | 1.2.11 |
| langgraph-checkpoint-postgres | 3.1.2 |
| Pydantic | 2.13.5 |
| Uvicorn | 0.52.4 |
| Celery | 5.6.3 |
| SQLAlchemy | 2.0.52 |
| langchain-core | 1.6.1 |
| Next.js | 14.2.5 |
| Node | v24.19.0 |
| React / antd / @ant-design/icons | 18.3.1 / 5.20.0 / 5.4.0 |
| Chrome（真实浏览器驱动） | 系统 Chrome（Playwright-core channel=chrome） |
| PostgreSQL（Compose 栈） | 17.11（`localhost:5432/langgraph`，客户证据 `evidence/services.json`） |
| Redis（Compose 栈） | 6.0.16（`localhost:6379`，PING 通过） |
| 工作区提交 | `561e97e`（main） |

---

## 三、测试统计

- **pytest：`120 passed, 1 skipped`**（`python -m pytest tests/`，venv，且 `DATABASE_URL=postgresql://user:pass@localhost:5432/langgraph`）。
- 在启用 PostgreSQL 后，**PostgreSQL 数据面 12 项全部通过**：`test_pg_store.py`（5，持久化/跨连接幂等/跨租户 404/联合唯一约束 + RLS 运行角色）、`test_pg_rls.py` + `test_pg_checkpoint_recovery.py`（7，RLS 租户隔离、TenantScopedCheckpointer 读/写/列举/恢复）。
- 仅 1 个 skip：`test_hardening_acceptance.py` 的 crewai 真实子智能体集成（`crewai` 未与 langgraph 同环境验证）。
- 关键新增：`tests/test_e2e_flow.py`（端到端全流程 + 角色数据可见性）、`tests/test_recovery.py`（SQLite 备份/恢复 RPO/RTO + Celery 任务注册）、`tests/test_sqlite_store.py::test_sqlite_concurrent_claim_single_winner`（并发审批 CAS 单赢家）。
- 并发修复：SqliteStore 单连接跨线程时 cursor 在锁外被其它线程 DML 失效 → 改为 `_q` 在锁内 `fetchall` 物化并返回带 `rowcount` 的结果对象，并发测试 12/12 稳定通过。

---

## 四、浏览器 / 反向代理链路验证证据

### 4.1 完整验收流程（真实浏览器）
`frontend/scripts/browser_e2e.mjs`（playwright-core + 系统 Chrome）驱动：**客户创建会话 → 查询政策 → 查询订单 → 发起退款 → 收到“等待审批” → 审批人二次确认通过 → 操作执行**。运行结果 `BROWSER_E2E_OK`。

| 步骤 | 截图 |
|---|---|
| 客户会话（含菜单/新会话/对话） | `evidence/screenshots/b02_chat_customer.png` |
| 退款触发审批（“等待审批”徽标） | `evidence/screenshots/b03_customer_approval_required.png` |
| 审批人待审批列表 | `evidence/screenshots/b04_approvals_pending.png` |
| 二次确认弹窗 | `evidence/screenshots/b05_approvals_confirm_modal.png` |
| 审批通过（操作 executed） | `evidence/screenshots/b06_approvals_approved.png` |

### 4.2 角色页面（真实浏览器自动登录）
`frontend/scripts/capture_pages.mjs`：
`01_login.png`、`07_workbench_admin.png`、`08_sessions_admin.png`、`09_settings_admin.png`、`10_workbench_agent.png`、`11_approvals_agent_readonly.png`。
- admin 可见菜单：客服工作台/会话历史/审批中心/成员与设置；`09_settings_admin.png` 展示租户成员（客户/客服/管理员/审批人）与状态。
- agent 进入审批中心仅只读（`11_approvals_agent_readonly.png`，提示“由 admin/approver 完成”）。

### 4.3 反向代理链路（HTTP + SSE）
浏览器访问 `http://127.0.0.1:3100`（Next 生产 `next start`），同源 `/api/*` 由 `next.config.mjs` rewrites 代理到后端 `http://127.0.0.1:8000`。验证：
- `GET /api/sessions`（经 `:3100`）→ 200；
- 完整流程（含 SSE `POST /api/chat`）经 `:3100` 通过：`scripts/live_e2e.py http://127.0.0.1:3100/api evidence/proxy_e2e.json` → `OK`，operation 状态 `executed`。
- 证据文件：`evidence/proxy_e2e.json`（经前端代理）、`evidence/http_e2e.json`（直连后端）、`evidence/e2e_flow.json`（pytest 端到端）。

后端 uvicorn 日志（`pwsh-21`，节选）：
```
POST /api/sessions 201
POST /api/chat 200
POST /api/approvals/22d5dbe9-.../decision 200
GET /api/operations/e5a7ce70-... 200
GET /api/members 200
```

---

## 五、RPO/RTO 与未验证边界

### 5.1 RPO / RTO
**A. 数据面冷备（SQLite，本地）** — `evidence/recovery.json`：
```json
{ "rpo_seconds": 0.0, "rto_restore_seconds": 0.000851, "backup_bytes": 73728, "scenario": "sqlite online backup API" }
```
- 使用 SQLite `Connection.backup()` 做在线备份 → 恢复为独立 store → 校验 session/operation/approval 全部还原，且已执行/已审批终态不丢失（`test_recovery.py`）。

**B. PostgreSQL 生产级 pg_dump/pg_restore（Compose，实测）** — `evidence/postgres_recovery.json`：
```json
{ "backup_seconds": 0.353, "restore_seconds": 1.963, "rpo_seconds": 0.0, "dump_ok": true, "restore_ok": true, "rows_seeded": 2, "rows_restored": 2 }
```
- 在 running postgres 容器内先写入 `_verify` 表 2 行 → `pg_dump -Fc` 备份 → 新建 `langgraph_restore_test` 库 → `pg_restore` → 恢复后 `count(*)=2`（RPO=0，备份点写入全部恢复）。RTO=1.96s（小库）。执行容器为 `multi_agent_e_commerce-postgres-1`，经 Docker 命名管道 `npipe:////./pipe/docker_engine` 驱动。

### 5.2 Docker Compose 实测（Redis/worker 重启 + 编排状态）
`scripts/docker_verify.py` → `evidence/docker_verification.json`：
- **编排状态**：`redis:7-alpine`、`postgres:17-alpine`、`frontend`、`api`、`worker` 5 个容器全部 `running`（前后一致）。
- **Redis 重启**：`restart` 后 `running=true`，`redis PING=true`，版本 6.0.16。**恢复验证通过**。
- **Worker 重启**：`restart` 后 `running=true`，日志显示 `Connected to redis://redis:6379/0` + 任务 `memory.write_tick` 注册。**重连恢复通过**。

### 5.3 未验证边界（坦诚声明）
| 项 | 状态 | 说明 |
|---|---|---|
| PostgreSQL 数据面（PostgresStore + TenantScopedCheckpointer + RLS） | **已验证** | Compose PostgreSQL 17.11；`test_pg_store/rls/checkpoint_recovery` 12 项全部通过（RLS 隔离、checkpoint 恢复、跨连接幂等）。 |
| Docker Compose 编排 | **已验证** | Compose 5 服务 running；经 Docker 命名管道 API 完成 Redis/worker 重启与恢复、pg_dump/restore。 |
| Redis 重启 / worker 重启 | **已验证** | 重启后恢复，worker 重连 `redis://redis:6379/0`；见 5.2。 |
| 生产 PostgreSQL RPO/RTO（pg_dump/restore） | **已验证（小库）** | RPO=0、RTO≈1.96s；大库/真实负载 RTO 未压测。 |
| 真实 LLM 端点 | **未验证** | 完全自托管端点未配置；本阶段全链路用 `MockLLM`（`LLM_MODEL=gpt-4` 进入能力矩阵白名单）。真实自托管 `llm_model`（如 `self-hosted-model`）不在白名单 → 写操作门控转人工（安全行为，正确）。 |
| 长期记忆（Graphiti + Neo4j） | **未接入/未验证** | 属后续阶段，依赖其版本/许可/自托管验证。 |
| CrewAI 子智能体 | **未验证（skip 1）** | `crewai` 未与 langgraph 同环境验证；本阶段用 Mock 层验证流程。 |
| 大库/高并发的 Postgres RTO、Redis 集群/持久化 AOF 开关 | **未压测** | 本次为功能/恢复验证；容量与持久化参数未做生产级压测。 |

---

## 六、结论

- **本阶段“创建会话 → 查询政策/订单 → 发起敏感申请 → 审批 → 查询操作结果”完整流程已在真实浏览器 + 后端 API（经前端反向代理）双线验证通过，并保留截图与 JSON 证据。**
- 前端已完成角色化菜单与数据可见范围（customer/agent/admin/approver）、客服工作台、审批中心（二次确认/状态刷新/操作查询）、SSE 断线恢复/事件去重/错误与人工升级展示。
- **PostgreSQL 数据面（PostgresStore + TenantScopedCheckpointer + RLS）已在本机 Compose PostgreSQL 17.11 上通过 12 项集成测试**；测试总数 `120 passed, 1 skipped`。
- **Docker Compose 编排 + Redis/worker 重启恢复 + Postgres pg_dump/restore（RPO=0/RTO≈1.96s）均已实测通过**（经 Docker 命名管道 API），见 `evidence/docker_verification.json`、`evidence/postgres_recovery.json`。
- **未完成/未验证项集中在：真实 LLM 端点、长期记忆（Graphiti+Neo4j）、CrewAI 子智能体，以及大库/高并发的生产级压测（RTO/容量/Redis 持久化参数）**。按工作纪律，这些不得宣称生产可用；本阶段不讨论生产部署，直至以上边界补齐证据。

*证据位置：`evidence/`（JSON + screenshots/）+ 后端 uvicorn 日志。*
