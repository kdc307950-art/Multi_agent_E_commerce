## 1. 开发环境配置

### 1.1 环境要求

- **CPython 3.12.x**（推荐安装最新的 3.12 补丁版本；作为本项目唯一的开发、CI 与容器运行时基线）
- Docker & Docker Compose
- Node 20+（仅用于构建 React + Next.js 前端）
- 至少 16GB RAM（仅文档演示可尝试 8GB；运行 PostgreSQL、Neo4j、Milvus 依赖、Redis、worker 和本地模型时建议 32GB）
- LLM API Key（OpenAI 兼容端点；完全自托管用本地/内网端点）
- Redis（Celery Broker / 缓存 / 分布式锁）
- PostgreSQL（检查点 + 业务数据）
- Milvus standalone（RAG 向量的本地/预发布验证拓扑；生产目标见《生产环境架构设计》）
- Neo4j（记忆图谱，长期记忆）

### 1.2 快速启动

```bash
# 克隆项目
git clone https://github.com/yourusername/after-sales-agent.git
cd after-sales-agent

# 创建并激活虚拟环境
# macOS / Linux
python3.12 -m venv .venv
source .venv/bin/activate

# Windows PowerShell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1

# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env，填入 LLM 端点 / 密钥等

# 初始化数据
python scripts/seed_data.py

# 启动本地开发基础设施（DB/向量/图库/Redis/Milvus 依赖）与 Celery worker
# Milvus 使用固定版本的 standalone Compose 拓扑（etcd + MinIO + standalone），不使用 latest；这不是生产 HA 部署。
docker compose up -d postgres etcd minio milvus-standalone neo4j redis worker

# 启动后端 API（开发态，可热重载）
uvicorn src.main:app --reload --host 0.0.0.0 --port 8000

# 构建/启动前端（新终端；React + Next.js）
cd frontend
npm install
npm run dev
```

> **端口分配**：后端 API `8000`、前端 `next dev` 默认 `3000`、PostgreSQL `5432`、Milvus `19530`、Neo4j `7474/7687`、Redis `6379`。**Compose 启动基础设施后不要再用本地进程重复占用 8000/5432**，避免端口冲突。

> **Python 版本策略**：固定 **CPython 3.12.x**。`crewai==0.152.0` 作为设计基线，必须在 CPython 3.12、当前 Pydantic 2.x 和本项目锁定依赖下完成安装、导入和最小 Crew 运行验收后，才可称为已验证；在验收完成前不得宣称兼容或生产可用。3.10、3.11、3.13 不作为本项目基线。

### 1.3 requirements.txt（Python 后端）

```text
# 版本基线（2026-09-03）：按官方文档与 PyPI 记录冻结候选段；安装后仍须在本项目环境做最小验收。
langgraph==1.2.11
langgraph-checkpoint-postgres==3.1.2
langchain>=1.0,<2.0
langchain-openai>=1.0,<2.0
langfuse>=2.50.0            # 自托管 Langfuse（后端链路/指标）
deepeval>=1.0.0
crewai==0.152.0             # 设计基线；需在 CPython 3.12 + Pydantic 2.x 下实测
pymilvus>=2.4.0             # Milvus 客户端；开发/预发布 standalone，生产连接 distributed/cluster
neo4j>=5.20.0
fastapi>=0.115.0
uvicorn>=0.30.0
pydantic>=2.0.0
sqlalchemy>=2.0.0
python-dotenv>=1.0.0
redis>=5.0.0               # Celery Broker / 缓存 / 分布式锁
celery>=5.3.0              # 异步任务队列（全 Python 栈）
psycopg[binary]>=3.1.0     # PostgresSaver / 业务库
psycopg-pool>=3.1.0         # 连接池
```

> **LangGraph 版本与 API 口径**：本项目以 `langgraph==1.2.11`、`langgraph-checkpoint-postgres==3.1.2` 作为 2026-09-03 的候选基线。官方事件流 API 使用 `astream_events(..., version="v3")`；不要把旧版 `v2` 示例混入该 API。`stream/astream` 的 stream-mode API 与事件流 API 不是同一个版本参数，必须按实际调用面分别核对。当前机器未发现可用 Python 解释器，因此上述版本仍标记为“候选基线/待本项目安装验收”，不得宣称已兼容或生产可用。

> **队列口径**：统一 Celery + Redis。**不使用 BullMQ/Node**，以保持全 Python 技术栈、与 LangGraph/FastAPI/CrewAI 同生态。

## 2. 分阶段实施路线图

### Phase 1: 核心流程搭建（第 1-2 周）

**目标**：完成 LangGraph 主控层基本路由和简单 RAG。

**任务清单**：
- [ ] 定义 `AgentState` 状态模型
- [ ] 实现 `classify_intent` 节点（LLM 意图分类）
- [ ] 实现 `route_by_intent` 条件路由
- [ ] 构建基础 RAG（Milvus standalone + 检索）
- [ ] 实现 `generate_response` 节点
- [ ] 配置 SqliteSaver 检查点
- [ ] 编写基础测试用例（5 个）

**验收标准**：
- 能识别"怎么退货？" → policy 意图 → RAG 检索 → 返回退货政策
- 检查点能保存和恢复状态

### Phase 2: 集成 CrewAI 子智能体 + 业务执行（第 3-4 周）

**目标**：开发三个 CrewAI 专家，并补齐退货/改址业务工具与执行节点。

**任务清单**：
- [ ] 设计模拟订单数据库（SQLite）
- [ ] 开发 `Order Analyst` Crew（订单查询）
- [ ] 开发 `Shipping Tracker` Crew（物流追踪）
- [ ] 开发 `Refund Specialist` Crew（退款处理）
- [ ] 实现 `process_return`（退货申请）与 `execute_return`
- [ ] 实现 `update_return_address` 与 `execute_address_update`
- [ ] 将 Crew 封装为 LangGraph 节点
- [ ] 实现条件路由（order/shipping/refund/return_request/return_address）
- [ ] 测试覆盖：每种意图至少 5 个用例

**验收标准**：
- "查询订单 ORD-001" → order 意图 → Crew 查询 → 返回订单详情
- "ORD-001 到哪里了" → shipping 意图 → Crew 追踪 → 返回物流信息
- 退款/退货/改址均进入唯一审批节点，无 direct 绕过

### Phase 3: Agentic RAG + 人工审批（第 5-6 周）

**目标**：实现自纠正 RAG，并把退款/退货/改址统一收口到人工审批。

**任务清单**：
- [ ] 实现查询改写节点
- [ ] 实现混合检索（向量 + 关键词）
- [ ] 实现相关性评分节点
- [ ] 实现自纠正循环（最多 3 次重试）
- [ ] 实现幻觉检测节点
- [ ] 实现唯一 `human_approval` 中断节点
- [ ] 实现审批恢复逻辑（按 `pending_action` 路由）
- [ ] 构建知识库（至少 20 篇政策文档）

**验收标准**：
- 模糊问题能自动改写后检索成功
- 检索结果不相关时自动重试；仍不合格转拒答/转人工
- 退款/退货/改址触发审批中断；只有审批通过才进入对应 `execute_*`

### Phase 4: 可观测性 + 评估体系（第 7-8 周）

**目标**：全链路追踪和自动化评估（完全自托管）。

**任务清单**：
- [ ] 部署自托管 Langfuse 并配置全链路追踪
- [ ] 为每个节点添加 `@observe` 装饰器
- [ ] 实现核心指标采集（Token、延迟、成功率）
- [ ] 构建评估测试集（≥50 个用例）
- [ ] 实现自动化评估（Faithfulness、Relevancy）
- [ ] 构建前端监控面板（React + Next.js）
- [ ] 编写评估报告

**验收标准**：
- 每次请求在自托管 Langfuse 可查看完整链路
- 评估测试集通过率作为项目自身基线指标，不作为外部承诺
- 仪表板显示实时指标

### Phase 5: 租赁服务器线上预发布与生产就绪验证（后续阶段）

**目标**：在项目账户下租赁的 IaaS 环境中验证真实网络、部署、隔离、故障恢复和容量边界；不以本地 Compose 成功代替生产证据。

**任务清单**：
- [ ] 冻结镜像、依赖锁文件、数据库/Graphiti/Milvus 版本与密钥管理方案
- [ ] 部署前端、API、Celery worker、模型端点、PostgreSQL、Neo4j、Milvus、Redis、Langfuse 和监控；数据面置于私网或受限安全组
- [ ] 使用至少两个租户的受控测试身份验证 API、会话、检查点、审批、RAG、图谱、缓存、队列和审计的隔离
- [ ] 验证 API/worker 重启、队列重复投递、模型不可用、审批超时和数据服务恢复时的安全回退
- [ ] 演练 PostgreSQL、Neo4j、Milvus 的独立备份与恢复；记录实际 RPO/RTO
- [ ] 对经业务批准的查询/审批/后台任务混合负载压测，记录 p95 延迟、吞吐、资源和扩容阈值
- [ ] 在生产拓扑中替换 standalone：Milvus 使用 distributed/cluster；PostgreSQL、Redis、应用和 worker 使用多副本；Neo4j 的集群或受控恢复策略按版本与许可确认

**验收标准**：
- 任意跨租户访问、跨租户 checkpoint 恢复、跨租户向量/图谱召回均为零容忍并有审计记录
- 敏感写路径在故障、重试或重启后零重复执行；不可安全继续时保留完整状态并转人工
- 备份恢复、故障恢复和容量结果形成可复核记录；未满足《生产基线与验收测试》中的 RPO/RTO 与业务 SLO 时，只能保留为预发布环境

## 3. 关键技术决策

### 3.1 为什么用 LangGraph 做主控而不是 CrewAI？

CrewAI 在复杂场景下存在**内部数据流和协作过程的黑盒化**问题，调试和透明性受限。LangGraph 通过**状态机模型**提供完整工作流可视化，提升可扩展性和可维护性。**采用混合架构作为设计选择，不引用外部"成功率/延迟倍率"数据作为本项目达标承诺**，最终指标以自建评估集实测结果为准。

### 3.2 为什么 CrewAI 不独立使用？

CrewAI 角色化协作能力强，但流程控制与全链路可观测有限；LangGraph 负责状态机与路由，CrewAI 承担子任务。两者结合，各取所长。

### 3.3 为什么 Agentic RAG + LangGraph RAG 结合？

传统 RAG 是"一锤子买卖"——检索错了也硬答。Agentic RAG 在流程中插入**检查点**：查询改写 → 检索 → 相关性评分 → 自纠正闭环。LangGraph 状态机天然适配这种**"检索-评估-调整"**的自纠正循环。

### 3.4 检查点策略

- 使用 **SQLite** 存储检查点（开发/原型；生产用 `TenantScopedCheckpointer + AsyncPostgresSaver`，见《生产基线与验收测试》）
- 每个 `thread_id` 对应一个工单会话
- 支持从任意检查点恢复（时间旅行调试）
- 检查点保留 **7 天**，自动清理（与会话过期策略保持一致）

## 4. 测试策略

### 4.1 单元测试

```python
# tests/test_graph.py
def test_intent_classification():
    state = {"messages": [{"content": "我想退货"}]}
    result = classify_intent_node(state)
    assert result["intent"] == "return_request"   # 退货申请（独立意图）
    assert result["confidence"] > 0.8


def test_approval_flow():
    """退款/退货/改址一律进入唯一审批节点。"""
    state = {"intent": "refund", "order_id": "ORD-001", "needs_approval": True}
    assert approval_route(state) == "human_approval"


def test_no_direct_bypass():
    """敏感操作不得存在 direct -> execute 绕过路径。"""
    for intent in ("refund", "return_request", "return_address"):
        assert route_after_tool(intent) == "human_approval"
```

### 4.2 集成测试

```python
# tests/test_e2e.py
def test_full_return_flow():
    client = TestClient(app)
    token = get_token("USER-001", active_tenant="TENANT-A")
    session_response = client.post("/api/sessions", headers={"Authorization": token})
    assert session_response.status_code == 201
    thread_id = session_response.json()["thread_id"]

    response = client.post("/api/chat", json={
        "mode": "start",
        "client_request_id": "REQ-001",
        "message": "我要退货，订单号 ORD-001",
        "thread_id": thread_id,
    }, headers={"Authorization": token})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_frozen_sse(response.text)
    assert events[0]["event"] == "accepted"
    assert any(event["event"] == "approval_required" for event in events)
```

> **鉴权修正**：端到端测试不再往 `/api/chat` 传 `user_id` 或 `tenant_id`，而是使用只含认证信息的令牌创建会话，并使用服务端返回的 `thread_id`。测试令牌中的 `active_tenant` 仅由受控测试身份提供方签发，不能映射为客户端请求字段。

### 4.3 多租户隔离测试

新增 `tests/test_tenant_isolation.py`，使用两个独立租户的受控测试令牌和同名业务资源验证以下契约：

- `test_same_tenant_session_is_accessible`：同租户用户创建的会话可正常对话；客服、管理员和审批人仅按该租户 RBAC 获得必要访问。
- `test_cross_tenant_thread_is_not_visible`：租户 B 使用租户 A 的 `thread_id` 调用 `/api/chat` 必须被拒绝（建议返回 404 以避免资源枚举），且图执行器不得被调用。
- `test_client_tenant_override_is_rejected`：body、query 或普通 Header 中携带 `tenant_id` 必须被 schema/入口守卫拒绝，不能覆盖服务端 `TenantContext`。
- `test_suspended_tenant_cannot_create_or_resume_session`：停用租户不能创建会话、继续对话、审批或消费新队列任务。
- `test_tenant_scoped_idempotency_does_not_collide`：两个租户的同名订单/请求 ID 可各自生成一次操作；同一租户的重复提交只重放不重复执行。
- `test_tenant_scoped_rag_cache_queue_and_graph`：Milvus 过滤、Neo4j/Graphiti 查询、缓存键和 Celery payload 均带 `tenant_id`，不得返回或消费另一租户数据。
- `test_cross_tenant_approval_is_rejected`：审批单、操作、订单、会话与审批人租户不一致时必须拒绝并写安全审计。
- `test_platform_admin_is_not_a_tenant_member`：平台级授权不能作为 `tenant_memberships.role` 写入，且不能直接调用 `/api/chat`、`/api/approvals/{approval_id}/decision` 或 `execute_*`；平台运维接口须验证独立的 `PlatformOperationContext` 与审计记录。

### 4.4 评估测试集

`tests/fixtures/test_cases.json` 包含**不少于 50 个**测试用例，覆盖：10 个退货申请、10 个退款、10 个物流查询、10 个政策咨询、5 个投诉、5 个边界/异常。每个用例含：`{"question": "...", "expected_intent": "...", "expected_keywords": [...]}`。

## 5. 部署配置

### 5.1 Docker Compose（基础设施 + worker）

```yaml
services:
  postgres:
    image: postgres:16
    environment:
      - POSTGRES_USER=user
      - POSTGRES_PASSWORD=pass
      - POSTGRES_DB=langgraph
    ports: ["5432:5432"]
    volumes: ["./data/postgres:/var/lib/postgresql/data"]

  etcd:
    image: quay.io/coreos/etcd:v3.5.5
    environment:
      ETCD_AUTO_COMPACTION_MODE: revision
      ETCD_AUTO_COMPACTION_RETENTION: 1000
      ETCD_QUOTA_BACKEND_BYTES: 4294967296
    command: etcd -advertise-client-urls=http://etcd:2379 -listen-client-urls=http://0.0.0.0:2379 --data-dir /etcd
    volumes: ["./data/etcd:/etcd"]

  minio:
    image: minio/minio:RELEASE.2024-02-17T01-15-57Z
    environment:
      MINIO_ACCESS_KEY: minioadmin
      MINIO_SECRET_KEY: minioadmin
    command: minio server /minio_data
    volumes: ["./data/minio:/minio_data"]

  milvus-standalone:
    image: milvusdb/milvus:v2.4.24
    command: ["milvus", "run", "standalone"]
    environment:
      ETCD_ENDPOINTS: etcd:2379
      MINIO_ADDRESS: minio:9000
    ports: ["19530:19530", "9091:9091"]
    volumes: ["./data/milvus:/var/lib/milvus"]
    depends_on: [etcd, minio]

  neo4j:
    image: neo4j:community
    ports: ["7474:7474", "7687:7687"]
    environment:
      - NEO4J_AUTH=neo4j/yourpass

  redis:
    image: redis:7
    ports: ["6379:6379"]

  worker:
    image: after-sales-agent
    command: celery -A src.tasks worker -l info
    environment:
      - REDIS_URL=redis://redis:6379
      - DATABASE_URL=postgresql://user:pass@postgres:5432/langgraph
      - VECTOR_DB_URI=http://milvus-standalone:19530
      - NEO4J_URI=bolt://neo4j:7687
    depends_on: [redis, postgres, etcd, minio, milvus-standalone, neo4j]
```

> 本 Compose 仅用于本地开发和单机预发布功能验证；后端 `api`、前端 `frontend` 不放入其中，避免与本地 `uvicorn`/`npm run dev` 端口冲突。线上预发布与生产 HA 拓扑见《生产环境架构设计》§4 和《生产基线与验收测试》。

### 5.2 环境变量 (.env)

```bash
# LLM（本地/内网 OpenAI 兼容端点，完全自托管）
LLM_PROVIDER=openai
LLM_API_KEY=sk-local
LLM_MODEL=your-self-hosted-model
LLM_BASE_URL=http://localhost:8001/v1

# Graphiti（仅在长期记忆验收闸门通过后启用；完全自托管必须关闭匿名遥测）
GRAPHITI_TELEMETRY_ENABLED=false

# 向量数据库
VECTOR_DB_TYPE=milvus
VECTOR_DB_URI=http://localhost:19530

# 可观测（自托管 Langfuse）
LANGFUSE_PUBLIC_KEY=pk-local
LANGFUSE_SECRET_KEY=sk-local
LANGFUSE_HOST=http://localhost:3002

# 检查点 / 业务库
DATABASE_URL=postgresql://user:pass@localhost:5432/langgraph

# 队列 / 锁
REDIS_URL=redis://localhost:6379
```

> **自托管约束**：默认使用项目方自管的本地/内网 LLM 端点与自托管 Langfuse；租赁 IaaS 部署时，端点应位于同一受控网络边界。若 `LLM_BASE_URL` 指向外部托管服务，必须披露 prompt/响应的数据流、合规影响和“不再满足完全自托管”的变化。

---

## 6. 成本估算（粗略）

单次售后工单处理约 **6,000 tokens**（意图识别约 500 / Agentic RAG 约 2,000 / CrewAI 执行约 3,000 / 回复生成约 500）。该数字是容量估算，不是已测量事实；必须按真实日志重新统计输入/输出 token、峰值并发和缓存命中率。

### 6.1 TCO 估算边界

| 成本项 | 原型最低口径 | 生产必须补充的测量 |
|---|---|---|
| 计算资源 | 16GB RAM 起步；本地模型另计 GPU/显存 | 峰值并发、p95 延迟、模型吞吐、扩容余量 |
| 租赁 IaaS | 预发布至少覆盖网络、磁盘、备份和监控所需实例；费用不是本地硬件的零成本替代 | 多副本应用、数据服务 HA、跨节点流量、快照/备份存储、带宽和公网入口的月度成本 |
| 存储 | PostgreSQL、Neo4j、Milvus、MinIO、etcd 持久卷 | 每租户日增量、备份副本、保留周期、恢复时间 |
| 运维组件 | PostgreSQL、Neo4j、Milvus 依赖、Redis、Celery、Langfuse | 监控、告警、升级、漏洞修复、值班人力 |
| 数据恢复 | 原型只验证备份命令可执行 | 明确 RPO/RTO，至少演练 PostgreSQL、Neo4j、Milvus 的恢复与租户级删除 |
| 模型成本 | 自托管不按云 Token 单价计费 | GPU/CPU 电力、折旧、并发排队和模型升级成本 |

**验收门槛**：没有实际压测、存储增长和恢复演练数据时，只能称为“设计估算”，不能写成固定月成本或生产容量承诺。
