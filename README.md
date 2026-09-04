# 电商售后多智能体工单系统 — 项目索引页（README · 生产候选版）

> **项目**：电商售后多智能体工单系统（E-commerce After-Sales Multi-Agent Ticket System）
> **一句话定位**：基于 **LangGraph 主控 + CrewAI 子智能体 + Agentic RAG + 人工审批** 的售后工单系统。当前已达成**生产候选基线**：完整源码 + 独立生产栈 `after-sales-prod` 实测全容器健康（api/worker/frontend/nginx/postgres/redis，api `/api/healthz`=200）+ PostgreSQL/RLS 数据面已接入并通过真实 PG 验收 + 发布 tag `release/v1.0.0-rc3`（→ 基线尖端，见 git tag）+ 镜像 digest 已记录。**正式生产放量当前为 `NO-GO`**——待受信 TLS、真实自托管 LLM 权重端点+评测、首批书面确认真实租户、7 天观察等**真实外部依赖（边界4）**到位后，转有条件 GO。本文档为项目入口/索引，按图索骥。
> **用途**：本目录为项目的**完整交付包**，可交作业、答辩、评审、部署。本文档为入口/索引。

---

## 一、文档一览（12 份正文 + 本索引）

| # | 文档 | 定位 | 核心内容 | 答辩亮点 |
|---|---|---|---|---|
| 1 | 项目需求说明书 | 要什么 | 功能需求(P0-P2)、非功能、约束、范围 | — |
| 2 | 项目说明书（Project Specification） | 怎么搭 | 项目结构、核心模块代码、数据模型、API | — |
| 3 | 项目架构说明书（Architecture Design Document） | 内部流程 | 四层嵌套、状态机、检查点、人工审批、Agentic RAG | LangGraph 状态机 + HITL |
| 4 | 项目落地细节（Implementation Details） | 怎么建 | 环境、路线图、关键决策、测试、部署、成本 | — |
| 5 | **记忆架构设计**（Memory Architecture Design） | 跨会话记忆 | **Graphiti**（时序图谱）+ Neo4j，validity window | 为什么选时序图谱而非 Mem0 |
| 6 | **工具调用与集成** | 工具层 | MCP 规范 + 工具清单 + **合并→分域→检索**三阶梯 | 为什么静态绑定最优，不上 Tool Search |
| 7 | **错误处理与回退机制** | 容错层 | 能力矩阵 + 写操作幂等 + 审批 + 降级阶梯 | 安全优先：先保证不做错资金操作 |
| 8 | **会话与线程管理设计** | 核心基础设施 | 不透明 thread_id、Sqlite→Postgres 检查点、多租户隔离、时间旅行 | 服务端 TenantContext + 会话归属校验 |
| 9 | **生产环境架构设计** | 部署拓扑 | 前后端分离、React+Next.js、FastAPI 网关、SSE、HA 数据面 | 开发/预发布/生产三层拓扑 |
| 10 | **生产基线与验收测试** | 范围与验收 | 本地/线上预发布/生产边界、回退基础设施、验收清单 | 以部署与演练证据判定生产就绪 |
| 11 | **前端体验与工作台设计** | 前端产品与工程 | 客服工作台、审批中心、SSE 状态、租户安全、API 契约冻结 | 可操作、可审批、可交接的工作台而非演示聊天框 |
| 12 | **LLM 端点接入与模型评测** | 模型接入 | 自托管 OpenAI 兼容端点、EndpointGuard、评测驱动白名单、脱敏 | 写白名单以真实端点评测结果驱动，非白名单写 fail-closed |

> 说明：①-④ 为纵向主干；⑤-⑫ 为横向专项/基础设施。生产基线与验收测试（⑩）冻结原型/生产边界，生产环境架构设计（⑨）描述部署拓扑，前端体验与工作台设计（⑪）约束前端产品与交互，LLM 端点接入与模型评测（⑫）约束模型接入与写白名单。

---

## 二、文档关系图

```
                用户需求（要什么）
                       │
     ┌─────────────────┼─────────────────┐
     ▼                 ▼                 ▼
① 需求说明书      ② 说明书(结构/API)    ③ 架构说明书(内部流程)
     │                 │                 │
     └────────────┬────┴─────────────┐
                  ▼                    ▼
              ④ 落地细节(路线/测试/部署/成本)

横向专项 / 基础设施（各管一层）
  ⑤ 记忆架构设计：Graphiti + Neo4j / validity window / 来源溯源
  ⑥ 工具调用与集成：MCP + 分域 + 三阶梯
  ⑦ 错误处理与回退：能力矩阵 + 幂等 + 人工审批 + 降级阶梯
  ⑧ 会话与线程管理：thread_id + TenantScopedCheckpointer + 归属校验
  ⑨ 生产环境架构设计：前后端分离 + SSE + HA 数据面 + 三层拓扑
  ⑩ 生产基线与验收测试：范围分层 + 回退基础设施 + 验收清单
  ⑪ 前端体验与工作台设计：客服工作台 + 审批中心 + 租户安全 + 契约冻结
```

**主干**：①要什么 → ②怎么搭 → ③内部怎么流转 → ④怎么建。
**专项/基础设施**：⑤-⑪——每个都是可独立答辩的差异化点。

---

## 三、推荐阅读路径

**评审/导师快速看**：① → ③ → ⑩（需求 → 内部架构 → 范围与验收）
**深入实现**：④ → ② → ③（落地路线 → 结构 API → 内部细节）
**答辩讲亮点（每个专项背一段）**：⑤ → ⑥ → ⑦ → ⑨ → ⑩ → ⑪（记忆 → 工具 → 容错 → 生产架构 → 生产/验收 → 前端工作台）

---

## 四、关键选型速查（全包统一口径）

| 维度 | 选定 | 说明 |
|---|---|---|
| 编排 | LangGraph 主控 + CrewAI 子智能体 | 状态机 + 角色化协作 |
| **检查点/会话** | 开发 `SqliteSaver` · 生产 **`TenantScopedCheckpointer + AsyncPostgresSaver`** | 会话归属和 checkpoint 的 invoke、恢复、历史、清理、审批续跑均校验租户作用域（见④/⑧） |
| 知识/RAG | Agentic RAG（自纠正 + 幻觉检测） | 政策问答，Milvus 向量 |
| **长期记忆** | **Graphiti（时序图谱引擎）+ Neo4j（社区版自托管）** | 用户画像 + "当时为真"溯源；用 Graphiti 引擎，非 Zep |
| **工具调用** | **MCP 规范**（主）+ 原生 Function Calling（本地小工具兜底） | 业务 7 + 平台 2，共 9 个工具能力 |
| **生产架构** | **React + Next.js + FastAPI 网关 + 前后端分离 + 多副本数据面** | 本地 Compose、租赁服务器预发布、生产 HA 分层演进 |
| **容错** | **能力矩阵 + 写操作幂等 + 人工审批 + 降级阶梯** | 安全优先，不做错资金操作 |
| 可观测 | 自托管 **Langfuse**（链路+指标） | 数据在项目方控制边界内流转；可选 Prometheus/Grafana |
| 部署 | Next.js 自建 runtime（SSR）或静态导出（Nginx）；后端容器（开发 Compose / 线上预发布 / 生产 K8s 或等效编排） | 前后端独立；租赁 IaaS 可承载项目自管环境 |
| 数据存储 | PostgreSQL + Neo4j + Milvus | standalone 仅用于开发/预发布；生产目标使用 HA 数据服务拓扑 |
| **租户隔离** | `TenantContext` + PostgreSQL RLS/等效 guard + 数据面命名空间 | API、会话、审批、RAG、图谱、缓存、队列和审计全链路带 `tenant_id` |

> **一句话架构**：LangGraph 编排 · Graphiti+Neo4j 给长期记忆 · TenantScopedCheckpointer+Postgres 给会话 · MCP 给工具 · TenantContext+数据面隔离给多租户 · 能力矩阵+审批/安全回退给写操作 · 三层部署与验收记录给生产证据。

---

## 五、文档约定

- 各文档**选型口径已统一**（见"关键选型速查"）；
- **开发/预发布/生产**：检查点（Sqlite→TenantScopedCheckpointer+Postgres）、部署（单机 Compose→租赁服务器线上验证→K8s/等效 HA）、鉴权（最小→完整 RBAC）均已区分标注；
- **多租户**：租户上下文由服务端认证和成员关系解析；`tenant_id` 不接受客户端覆盖，并贯穿 PostgreSQL、RAG、图谱、缓存、队列、审批与审计；跨租户默认拒绝；
- **完全自托管**：LLM、向量库、图谱、数据库和可观测均由项目方部署、配置、备份和运维；可使用项目账户下的租赁 IaaS。若接入外部 LLM 或云端可观测，必须单独披露数据流、合规和责任边界；
- **长期记忆 = Graphiti（开源时序图谱框架）+ Neo4j**；Graphiti 可自托管，但本项目**尚未接入** Graphiti/Neo4j 运行代码，其集成、租户过滤和恢复能力仍待后续环境验证（详见《记忆架构设计》；当前状态见 §六"长期记忆＝未接入"）；
- 每份专项文档末尾含**答辩叙事 / 追问应答**，可直接背。

---

## 六、当前落地状态（生产候选版 · 2026-09 盘点）

> 本节用词遵守工作纪律，按**四种证据边界**严格分层：**边界1 单元测试**（pytest/内存/SQLite）、**边界2 Mock/沙箱**（preview mock LLM、sandbox_gateway、沙箱并发）、**边界3 PostgreSQL/RLS 实测**（真实 PG 数据面，多为一**次性独立测试库**，非 `after-sales-prod` 专栈实机）、**边界4 真实生产外部依赖**（真实受信 CA/域名、真实权重模型端点、真实资金渠道、书面确认真实租户、7 天观察）。绝不把未验证内容写成"生产可用"，也不把"一次性库取证"当"生产栈实机达标"。

| 项 | 状态 | 证据 / 说明 |
|---|---|---|
| 后端骨架 | **本地可运行** | `src/` 提供 FastAPI + LangGraph 主图 + 内存存储 + Mock LLM；`uvicorn src.main:app` 可启动 |
| 最小闭环 | **本地已验证**（边界1） | 认证上下文 → 创建会话 → `POST /api/chat` SSE → 意图/审批分流 → 审批决定 → 操作状态查询 → 审计 |
| 自动化测试 | **全量 pytest：390 passed, 34 skipped**（424 collected，54.63s，EXIT=0；**发布基线环境实测**）。34 skipped = **33 项 PostgreSQL 数据面测试**（无 `DATABASE_URL`，需启动 PostgreSQL 并设置 `DATABASE_URL` 后运行）+ **1 项 CrewAI 真实调用链测试**（`test_hardening_acceptance.py:384`，本环境未安装 crewai 故跳过）。此前记录的 `320 passed, 34 skipped, 70 errors` 均为**受限沙箱清理 `tmp_path` 的 `PermissionError [WinError 5]` 环境权限问题**所致（70 errors 均属环境问题、0 个真失败；发布基线环境无此限制），故修正后实测 390/34。 | `pytest tests/`：认证/租户隔离、SSE 契约、审批幂等、RAG 状态隔离、跨租户拒绝、OpenAPI 契约锁死、SQLite 持久化、执行引擎并发、沙箱网关、Postgres/RLS 数据面（边界1/3）。测试默认写系统临时目录，不污染 `evidence/`；`DSH_EVIDENCE_DIR` 显式指定时由独立验收脚本生成正式证据。 |
| 存储后端 | **memory + sqlite + postgres 三后端**（边界1/3） | 默认 memory；`STORAGE_BACKEND=sqlite` 本地持久化已验证；`PostgresStore + TenantScopedCheckpointer + RLS` **已接入并通过真实 PG 验收**（`PG_ACCEPTANCE_REPORT.md` 数据面 12 项通过、RLS FORCE 验证） |
| 依赖验收 | **部分完成** | `langgraph==1.2.11` / `langgraph-checkpoint-postgres==3.1.2` 已导入+最小运行验收；`crewai==0.152.0` 在独立 venv 导入+对象构造验收通过，**未与 langgraph 同环境验证共存** |
| 前端 | **容器内构建成功** | `docker build frontend` 成功（Next.js 14.2.5 `Ready`，`/` 返回 200）；本机 npm 受安全策略限制，故在容器内构建验证 |
| Docker Compose（preview） | **本机实机验证** | 已 `docker compose up` 启动 postgres:17-alpine/redis:7-alpine/api/frontend/worker 并验证：postgres `SELECT version` 通过、redis `PONG`、api `:8000` openapi 200 + 退款触发 `approval_required`、frontend `:3000` 200 |
| **独立生产栈 `after-sales-prod`** | **全容器健康（边界3 专栈实机）** | `PROD_STACK_HEALTH.md`（T7）：compose `migrate` exit 0 + api/worker/frontend/nginx/postgres/redis 全 **healthy**，nginx `8080`/`8843` 暴露，api `/api/healthz`(8843)=200、frontend `/`=200、`/api/metrics`(公网)=404（内网化正确阻断）；受信 TLS 就绪前不可对外暴露 8080/8843 |
| PostgreSQL/RLS | **已接入并验证（边界3）** | `PostgresStore + TenantScopedCheckpointer + RLS` 已入库并经真实 PG 验收（数据面 12 项通过）；`DEPLOY_BASELINE` §6.2/PROD_STACK_HEALTH：生产栈 RLS FORCE + 租户 policy 生效；`MIGRATE_VERIFY` 全新 prod-like 库 clean migrate exit 0（17 表、复合 FK、无 InvalidForeignKey）|
| 发布基线 | **已确立（边界1/3）** | `release/v1.0.0-rc1` → `cd743d3`（历史）、`release/v1.0.0-rc2` → `bca4861`（历史、不可移动）、**`release/v1.0.0-rc3` → 基线尖端**（当前发布候选；含迁移修复 `696444a`、`BUSINESS_DATA_BACKEND` `cd743d3`、阶段一 rc3 收口——测试默认写临时目录 + 真实 390/34 口径 + uv.lock 不入库）；镜像 digest 已记录（api `5d39f030...`、frontend `12c35ff7...`，工作树构建、字节级 clean-context 重建=部署期执行项）；功能基线 `7941246` |
| 长期记忆（Graphiti/Neo4j） | **未接入** | 属后续阶段，依赖其版本/许可/自托管验证，不承诺（设计态） |
| 能力矩阵 / CrewAI 子智能体 | **Mock 层验证（边界2）** | 写操作门控与审批流在工作流层验证；真实 `crewai==0.152.0` 子智能体集成待验收 |
| **自托管 LLM 端点接入** | **代码就位 + 本地验收（边界2）+ 真实权重 BLOCKED（边界4）** | `src/llm/self_hosted_server.py`、`EndpointGuard`（网络白名单）、脱敏日志、评测驱动白名单；`verify_llm_chain.py` 三验收项通过。**真实模型权重尚未接入**（边界4 BLOCKED），写白名单需以真实端点跑 `scripts/evaluate_models.py` 后按报告注入 |

**结论（生产候选版）**：系统**已达成生产候选基线**——PostgreSQL/RLS 数据面已接入并验证、独立生产栈 `after-sales-prod` 全容器健康、发布基线忠实（**rc3 以基线尖端为锚**）、镜像 digest 可复现记录。**正式生产放量当前 `NO-GO`**，受**边界4 真实外部依赖**阻断：受信 TLS（A9/G10）、真实自托管 LLM 权重端点+评测（A10/G5）、首批书面确认真实租户（A1/G13）、7 天观察（A11/G14）、真实资金链路（A5/G6）。外部输入到位 + 生产栈运行时 RLS/告警复验 + clean-context 字节级重建后转有条件 GO（详见 `evidence/prod-go-live/release-manager/GO_NO_GO.md`）。

---

*版本 2.0 · 2026-09-04 · 生产候选版项目索引 · 全包 12 份正文 + 本文档 + 发布证据链（`evidence/prod-go-live/`）。*
