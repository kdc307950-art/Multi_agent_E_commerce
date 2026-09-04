# 正式生产上线 Go/No-Go 清单（定稿）

> **产出国角色**：release-manager（发布经理）· prod-go-live 团队 · 任务 t6
> **版本状态**：**定稿**（基于 t1–t5 各角色回填的真实证据；结论列与整体裁决已定稿）。
> **证据来源**：`evidence/prod-go-live/<role>/`（deploy-engineer/security-auditor/test-runner/observability-engineer/acceptance-engineer），
> 并交叉引用仓库既有 `[substrate]`（preview/沙箱级）证据作为能力基线。
> **诚实原则**：凡**真实受信 CA/真实域名/真实权重模型端点/真实资金渠道/首批书面确认租户/7 天观察**这些外部输入未获提供，
> 一律如实标注 `❌ BLOCKED-需外部`，绝不虚设达标；凡"完整 prod 栈带起健康 + 运行时告警复验 + 对账/指标重建"未实际执行的，
> 一律标注为部署期执行项。
> **定稿依据说明（git 实测，唯一事实来源）**：
> **基线 commit 链**：`7941246`（已测功能）→ `ad168ef`/`8be2d97`/`03819a8`（可复现构建）→ `3268a1c`（clean-context 断言）→ `696444a`（migrations 复合外键修复）→ `8ddca48`（接受运行面+部署配置）→ `cd743d3`（`BUSINESS_DATA_BACKEND=postgres`）→ `cde30fb`（T1/T7 发布基线证据+迁移修复复验+生产栈健康）。
> `release/v1.0.0-rc1` = `cd743d3`（annotated tag 对象 `2120c4b`，peel 到 commit `cd743d3`）。`release/v1.0.0-rc2` = **基线尖端**（历史基线 `59e37f2`(rc2 定稿) 之上叠加阶段一发布基线收口提交；以 tag 为锚）。当前 HEAD = **基线尖端**（阶段一已收口提交，以 `release/v1.0.0-rc2` 为锚）。
> **工作区 `git status --porcelain` clean**（阶段一已提交收口：统一发布口径 + 归档证据 + 清理临时产物）。运行面源码与部署配置已入 `cd743d3`/`cde30fb`/`59e37f2`；本次 rc2 校订项 `README.md`（生产候选口径）与 `IMAGE_DIGESTS.json`（rc2 口径）已随阶段一收口提交纳入。
> 镜像 digest 为**工作树构建、非字节级可复现**（见 G2 边界标注）；后续 "rc2 commit + `git archive` clean-context 重建" 列为部署期执行项。

- 记录 ID：`GONOGO-PROD-20260904`
- 定稿时间：2026-09-04（t6）
- 目标环境：`production`（正式生产上线，非 preview）
- 依据模板：`deploy/records/PRODUCTION_DEPLOYMENT_record-TEMPLATE.md`、`CANARY_TENANTS-TEMPLATE.md`、`SCALEUP_APPROVAL-TEMPLATE.md`、
  `TEMPLATE-rollback.md`、`TEMPLATE-pg-backup-restore.md`、`OPS_RUNBOOK.md`、`PRODUCTION_ACCEPTANCE_VERIFICATION.md`

---

## 〇、四种证据边界（本清单统一口径）

> 凡判定均标注其证据所属证据边界，绝不混淆，避免把"一次性库取证"误作"生产栈实机达标"或把"mock 引擎"误作"真实模型能力"。

| 边界 | 定义 | 证据示例 | 证据边界内可信度 |
|:---:|------|---------|----------------|
| **边界1 单元测试** | `pytest tests/` 纯单测（内存/SQLite，不触真实 PG/外部） | `tests/test_approval_idempotency.py`/`test_tenant_isolation.py`/`test_llm_endpoint_gate.py` 等 | 契约/逻辑层 |
| **边界2 Mock / 沙箱** | preview 层 mock LLM、`sandbox_gateway` 沙箱、沙箱并发压测、真实 HTTP 沙箱回执 | `evidence/sandbox_concurrency_stress.json`、`acceptance-engineer/LLM_GATEWAY_ACCEPTANCE.md`、`tests/test_sandbox_e2e_flow.py` | 系统层行为（非真实资金/权重） |
| **边界3 PostgreSQL / RLS 实测** | 真实 PostgreSQL 数据面（动态 B1–B5、真实 PG 并发、DR 加密恢复、全新库 clean migrate） | `security-auditor/rls_dynamic_probe.json`、`test-runner/concurrency_stress_pg.json`、`DR_encrypted_restore.md`、`deploy-engineer/MIGRATE_VERIFY.md` | 数据面真实性（对象多为一**次性独立测试库**，非 `after-sales-prod` 专栈实机） |
| **边界4 真实生产外部依赖** | 真实受信 CA/域名、真实权重模型端点、真实资金渠道、书面确认真实租户、7 天观察 | 无（外部输入未提供）——均 **BLOCKED-需外部** | 仅外部输入到位后成立 |

> **诚实声明**：边界3 的"真实 PG"取证对象多为 **preview 集群 + 一次性独立测试库**（`langgraph_rls_audit__*`/`langgraph_drill`/`after-sales-drill-pg`），属真实 PG 数据面技术真实性，但**非 `after-sales-prod` 专栈实机**；生产栈实机复验列入部署期执行项。边界2 的"真实运行态告警"用受控合成钻取源（`drill-probe`）注入，非真实流量；"可写模型"跑在 mock 引擎，**不构成真实模型能力证明**。

---

## 一、门禁总表（定稿）

### 1.1 门禁总表（结论 + 责任 + 证据边界）

| # | 门禁 | 要求 | 结论 | 责任 | 证据边界 |
|---|------|------|:---:|------|:---:|
| G1 | **代码 tag（生产发布基线）** | 正式生产发布 tag + 干净树 + 完整 commit；含迁移修复与全部已接受运行面 | **✅ 实测** | deploy-engineer | 边界1/3 |
| G2 | **镜像 digest（可复现构建）** | 记录镜像 digest；内容/配置确定性；冷构建可复现 | **⚠️ 部分**（内容/配置确定性 ✅；字节级冷构建【未复现＝部署期执行项】） | deploy-engineer | 边界2 |
| G3 | **RLS 动态验证** | FORCE RLS + NOBYPASSRLS；跨租户读/写/改 tenant_id 零可见/被拒 | **✅ 实测** | security-auditor | 边界3 |
| G4 | **回调并发/幂等** | 同 operation/同幂等键并发回调不重复执行（submit 恰 1 次） | **✅ 实测** | test-runner | 边界1/2/3 |
| G5 | **真实模型评测 + 能力矩阵** | 评测驱动白名单；非白名单写 fail-closed；**真实权重模型评测通过** | **❌ BLOCKED**（真实模型评测）+ 能力矩阵/写门控 **✅ PASS** | acceptance-engineer | 边界1/2（能力矩阵）＋边界4（真实模型） |
| G6 | **真实网关沙箱验收** | self-hosted 网关 live 链路可跑通且不重复执行/可对账；生产网关沙箱可用 | **⚠️ 部分**（代码/测试 PASS；生产网关沙箱＝部署期执行项） | acceptance-engineer | 边界1/2 |
| G7 | **恢复/备份演练** | RPO≤15min；RTO≤60min；加密备份+最小权限角色+SHA-256+marker | **✅ 实测**（RPO=43.78s 周期界定、RTO=0.643s） | test-runner | 边界3 |
| G8 | **告警演练** | 规则可触发/可定位/可关闭；关键指标有界打点 | **⚠️ 部分**（RpoExceeded 真实闭环；其余需当前构建运行时复验；含 6.1/6.2/6.3 修复项） | observability-engineer | 边界2（RpoExceeded 合成钻取）＋边界1（求值器等价） |
| G9 | **生产密钥/库/备份独立性** | 生产独立密钥/库/备份位置/证书，不复用 preview | **✅ 实测**（配置级 independent=true + 完整 prod 栈带起健康已证实）+ ⚠️ 生产栈运行时 RLS/告警复验＝部署期执行项 | deploy-engineer | 边界3 |
| G10 | **受信 TLS** | 生产使用受信 CA 证书链（非自签名） | **❌ BLOCKED-需外部** | deploy-engineer | 边界4 |
| G11 | **人工审批强制** | 退款/退货/改址必经唯一 human_approval，无 direct 绕过 | **✅ 实测** | security-auditor | 边界1/3 |
| G12 | **只读+shadow 先于 live** | 先只读与 shadow 观察，再切换 live；切换受控 | **✅ 有序** | release-manager | 边界2 |
| G13 | **首批真实租户书面确认** | 首批真实租户书面确认函签署 + 白名单注入 + 成员/角色授予 | **❌ BLOCKED-需业务方** | release-manager / 业务方 | 边界4 |
| G14 | **7 天连续观察** | 切换 live 后连续观察 ≥7 天 | **❌ 待切 live 后** | observability-engineer | 边界4 |
| G15 | **对账可核实** | 非终态收口；不一致/不可核实→转人工；mismatch 计数可观测 | **⚠️ 部分**（入口/转人工/留痕 ✅；mismatch 计数后台未实测） | test-runner | 边界2/3 |

> **证据边界口径**：G3/G7/G9 属边界3（真实 PG，对象为一**次性独立测试库**，非 `after-sales-prod` 专栈实机）；G2/G8/G12 属边界2（mock/沙箱/合成钻取）；G5/G6 的能力矩阵部分属边界1/2，其真实模型/真实资金部分属边界4；G10/G13/G14 全部属边界4（BLOCKED-需外部）。详见 §〇。

---

## 二、逐项详表（定稿）

### G1 代码 tag（生产发布基线）—— ✅ 实测（基线忠实，rc1 精确=cd743d3）
- **判定**：发布基线忠实。**已测功能基线**＝`7941246`（baseline-prod-1）。**可复现构建链**含 `ad168ef`/`8be2d97`/`03819a8`/`3268a1c`；迁移修复 `696444a`（`fix(migrations): shipping_events 复合外键修复`，全新 prod-like 库 clean migrate exit 0）已纳入；运行面源码+部署配置已入 `8ddca48`，`cd743d3` 补 `BUSINESS_DATA_BACKEND=postgres`，`cde30fb` 补发布基线/生产栈健康证据。**`release/v1.0.0-rc1`精确指向 `cd743d3`**（tag 对象 `2120c4b`），此后 HEAD 前进至 `cde30fb`（新增 DEPLOY_BASELINE/PROD_STACK_HEALTH/IMAGE_DIGESTS/BASELINE_COMMIT_SCOPE 4 份证据），再前进至 `59e37f2`（rc2 定稿），再叠加**阶段一发布基线收口提交**（基线尖端，当前 HEAD）。
- **本次 rc2**：`release/v1.0.0-rc2` 以**基线尖端**为锚（历史基线 `59e37f2`(rc2 定稿)）。**git 状态 clean**：阶段一发布基线收口提交已纳入（统一口径+归档证据+清理临时产物）。
- **证据**：`evidence/prod-go-live/deploy-engineer/git-baseline.txt`、`DEPLOY_BASELINE.md`、`IMAGE_DIGESTS.json`（release_baseline）；本会话 `git rev-parse`/`git rev-list`/`git status --porcelain` 复核（提交后 0 条）。
- **诚实边界**：`DEPLOY_BASELINE.md` §1.1 "HEAD=3268a1c" 为 t1 早期快照；以 `cd743d3`/`cde30fb`/`59e37f2` 及**基线尖端**为准。**rc1 本身不含 `cde30fb` 的 4 份发布证据**——正式发布 tag 为 `release/v1.0.0-rc2`（**基线尖端，以 tag 为锚**）。

### G2 镜像 digest—— ⚠️ 部分（内容/配置确定性 ✅；字节级冷构建【未复现 = 部署期执行项】）
- **判定**：镜像 digest 已记录且**内容/配置确定性达成**；deploy 管线（base 钉 digest + 依赖 `==`/lock + `git archive` 固定 mtime + `SOURCE_DATE_EPOCH` + `--pull` 层缓存）设计上**可复现**。**⚠️ 字节级冷构建一致性未在本会话实证**：① Docker engine 对当前非提升 token 拒连（`npipe:////./pipe/dockerDesktopLinuxEngine` `permission denied`），**无法真实重跑 `git archive` clean-context 构建**；② 既有 BuildKit（本环境 buildx）亦存在"同上下文两次 `--no-cache` 冷构建 digest 不同"非确定限制（`--reproducible` 在本 buildx 不可用）。因此 **字节级冷构建列为部署期执行项/BLOCKED-需 Docker engine 可连接**，属"字节级复现残余风险"，非内容/逻辑阻断。
- **证据（本 rc2 观测值，工作树构建、代码==基线）**：`evidence/prod-go-live/deploy-engineer/IMAGE_DIGESTS.json`：
  - `api/worker/migrate`：`after-sales-prod-api@sha256:5d39f030697e1b00ef83fb22a3358ddd2f923be6752a082d9f31d20f4039f293`（**含 migrations 复合外键修复重建**，base `python:3.12-slim@sha256:78387bc...` 钉定）。
  - `frontend`：`after-sales-prod-frontend@sha256:12c35ff738c4b29fba0562c7677d086ab15516cbcaf6a2b53f77dc22b2886cad`（base `node:20-alpine@sha256:fb4cd12c...` 钉定）。
- **诚实备注**：以上为**工作树构建、非字节级可复现、非 `git archive` clean-context 重建产物**；代码与基线一致（含迁移修复）。**切勿视为"已 clean-context 字节级复现"**。部署期在具备 engine 访问环境跑通 `git archive release/v1.0.0-rc2 | tar -x` + `compose build --no-cache` 后，需刷新 IMAGE_DIGESTS.json 并改为"已 clean-context 字节级复现"。与 `DEPLOY_BASELINE` §7#2（rc1"⚠️ 待干净重做"）同款边界。

### G3 RLS 动态验证—— ✅ 实测
- **判定**：真实 PostgreSQL 动态 B1–B5 全拦。
- **证据**：`evidence/prod-go-live/security-auditor/RLS_ISOLATION_REPORT.md` §一 + `rls_dynamic_probe.json`（conclusion=true）+ `pg_rls_inventory_live.json`。B1 跨租户直连查询 0 行；B2 跨租户写 `WITH CHECK` 拒；B3 改 tenant_id 拒（原行零污染、跨租户仍 0）；B4 `app_runtime` NOBYPASSRLS 非超管；B5 FORCE RLS 生效（checkpoints/checkpoint_blobs/checkpoint_writes/checkpoint_thread_scopes 四表 RLS enable+force）。
- **取证口径**：RLS 动态取证用 **preview 集群 + 一次性独立测试库**（`langgraph_rls_audit__...`，已 drop+REVOKE，live `langgraph` 未污染，见报告 §〇/§九）——属**边界3**（真实 PG 数据面，非 `after-sales-prod` 专栈实机）。生产栈 `after-sales-prod` 容器级健康**已证实**（PROD_STACK_HEALTH.md），但**生产栈数据面 RLS 动态复验**仍列**部署期执行项**（DEPLOY_BASELINE §7#8）。

### G4 回调并发/幂等—— ✅ 实测
- **判定**：同 operation 并发 N=256 → `provider.submit=1`、execution record=1、终态封闭；跨租户同幂等键互不覆盖；同 nonce CAS 仅 1 confirmed。
- **证据**：`evidence/prod-go-live/test-runner/` `concurrency_stress.json`（SQLite N=256）、`concurrency_stress_pg.json`（**真实 PostgreSQL** N=256）、`sandbox_concurrency_stress.json`（真 HTTP 沙箱，含 FAIL 路径收敛单一 `compensated`）；`test_pg_callback_concurrency.py`（真实 PG+RLS，7 passed）。
- **取证口径**：同 G3，对象为 preview + 一次性独立 PG（真实 PostgreSQL 数据面，边界3）；生产栈容器级健康已证实，但**生产栈数据面并发幂等复验**为部署期执行项（DEPLOY_BASELINE §7#8）。

### G5 真实模型评测 + 能力矩阵—— ❌ BLOCKED（真实模型评测）＋能力矩阵/写门控 ✅ PASS
- **判定**：① **真实模型评测＝`❌ BLOCKED-需外部（真实权重端点）`**：preview 运行时 `LLM_BACKEND=mock`、`LLM_BASE_URL=http://model-endpoint:8001/v1` 无对应容器、host 探测 `127.0.0.1:8001/v1/models` → `WinError 10061（连接被拒绝）`、`self-hosted-model` 为 `MockLLM` 规则引擎代表（非真实权重）；`evidence/llm_candidate_eval.json` 现仅 `self-hosted-demo` stub（write_op_pass=true，cases=1），先前全量 24 用例报告**不在磁盘**。② **能力矩阵/写门控＝`✅ PASS`**：受限环境空白名单/缺报告 → `frozenset()` fail-closed；`capability_ok(self-hosted-demo)=True`、`self-hosted-model=False`；非白名单拒写转人工；`tests/test_llm_endpoint_gate.py` 21 passed。⚠️ 但可写模型跑在 **mock 引擎**，**不构成真实模型能力证明**；`verify_capability_matrix_t2.py` 因报告缺 `self-hosted-model` 有 4 项 FAIL（报告完整性，非逻辑缺陷）。
- **差异根因（诚实标注 · 阶段一只标注、不重生成）**：`evidence/llm_candidate_eval.json` 的 `self-hosted-demo`/1‑用例 stub **并非真实评测产物**，而是被测试 `tests/test_llm_endpoint_gate.py::test_whitelist_model_still_requires_approval` **覆写共享证据文件**所致（该测试为自测"白名单→仍进审批"直接 `json.dump` 写入真实报告路径 `evidence/llm_candidate_eval.json`；运行 pytest 即清空真评测报告）。**权威目标口径** = 模型 `self-hosted-model` + 全量 24 用例（`src/config.py` 默认、`.env.*.example`、端点 `self_hosted_server.py`、`evaluate_models.py` 均默认 `self-hosted-model`；`src/llm/eval/cases.py` ALL_CASES 合计 = 8+4+2+1+2+2+5 = 24）。**真实重生成推迟到阶段三**：需① 真实权重端点先起 + ② 先用 mock 对 `self-hosted-model` 产口径一致的报告 + ③ **必须先修上述覆写证据的测试**（否则再跑 pytest 会再次清空）；三道都满足后才可宣称"已用真实权重模型评测"。当前（阶段一）只做诚实标注，**不宣称已验证**。
- **证据**：`evidence/prod-go-live/acceptance-engineer/LLM_GATEWAY_ACCEPTANCE.md` §1/§2 + `LLM_GATEWAY_EVIDENCE.json`；`evidence/llm_candidate_eval.json`（stub）。
- **解锁条件**：①（先）修复 `tests/test_llm_endpoint_gate.py::test_whitelist_model_still_requires_approval`——改写临时/受控报告路径或测试后恢复原文件，避免 pytest 覆写权威证据；② 部署真实自托管权重端点（vLLM/Ollama 或自管 OpenAI 兼容服务）→ 配置 `LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL` + 写入 `LLM_ALLOWED_HOSTS` → `LLM_BACKEND=openai_compatible` → 跑 `scripts/evaluate_models.py --base-url <真实端口> --model-name self-hosted-model` 产出全量 24 用例 `write_op_pass=true` 报告 → `HIGH_CONFIDENCE_MODELS` 显式列出 `self-hosted-model`。

### G6 真实网关沙箱验收—— ⚠️ 部分（代码/测试 PASS；生产网关沙箱＝部署期执行项）
- **判定**：网关沙箱链路**代码/测试级 ✅ PASS**（服务端 SQLite 幂等 `UNIQUE(tenant_id,idempotency_key)`+`ON CONFLICT`、回调验签、compensate 稳定 reversal_id、reconcile→`MISMATCHED`→`HUMAN_HANDOFF`、`gateway_unconfigured` fail-closed、N=256 单次提交、`tests/test_sandbox_e2e_flow.py` 14 passed）；**但"生产网关沙箱可用"＝部署期执行项/未部署**（preview `EXECUTION_PROVIDER=mock`、无 `GATEWAY_BASE_URL`、无生产网关容器）。
- **证据**：`evidence/prod-go-live/acceptance-engineer/LLM_GATEWAY_ACCEPTANCE.md` §3 + `LLM_GATEWAY_EVIDENCE.json`。
- **解锁条件**：部署内网 `sandbox_gateway` → 注入 `GATEWAY_BASE_URL`/`GATEWAY_API_KEY` → `EXECUTION_PROVIDER=sandbox_http` → 端到端复跑 + shadow-only 提交。

### G7 恢复/备份演练—— ✅ 实测（RPO 周期界定）
- **判定**：RPO=**43.780s**（≤900s 周期上界）；RTO=**0.643s**（≤3600s）。加密备份（aes-256-cbc+PBKDF2+`Salted__` 密文头部非明文 PGDMP）+ SHA-256 校验 + 最小权限 `backup_role`（BYPASSRLS 只读、`has_select_all=17`、`has_dml_all=0`）+ 异机副本落**`data/prod-backups`（prod 独立目录）** + 恢复校验（marker_in_restore=0、TENANT-A/B 保留、RLS policy 计数=15）。
- **证据**：`evidence/prod-go-live/test-runner/DR_encrypted_restore.md`、`drill_pg_encrypted_restore.json`、`CONCURRENCY_RECOVERY_REPORT.md` §4（`backup 文件=langgraph-20260904142219.dump.enc 55648 bytes`；`restore_RTO=0.643223s`）。
- **边界（如实）**：RPO 口径为 **pg_dump 周期备份**（`BACKUP_INTERVAL_SECONDS` 缺省 900s 界定上界），**非 PITR/WAL 连续归档**；如需秒级 RPO 需另配 WAL 归档。演练对象为 preview + 一次性独立库（边界3，真实 PG 数据面，非 `after-sales-prod` 专栈实机）；生产栈容器级健康已证实，但**生产栈恢复演练（`verify_dr_compose.sh` 容器实跑 + 每日恢复演练）**为**部署期执行项**。DR 密钥为**一次性测试密钥**（`.backup_key`，生产删除且不复用）。

### G8 告警演练—— ⚠️ 部分
- **判定**：① `RpoExceeded` **真实运行态端到端闭环**（受控注入超阈值 → pending → firing → PromQL 定位 → 恢复 clear），证据为真实 Prometheus `/api/v1/*`；② 关键指标**当前代码**有界标签/`_FORBIDDEN_LABELS`（9 个高基数字段均被拒）/无重复 `# TYPE` 验证通过；③ **其余基于有界计数器的规则**（ApiHighErrorRate/ApprovalFailureRateHigh/HumanInterventionRate 等）运行态无法真实触发（**运行中 preview api 为旧构建，缺 `status`/`human_intervention_total` 等标签**），已用求值器等价证据覆盖，标记为**部署期复验项**；④ 含 **3 处高优先修复项**：§6.1 运行态 Prometheus 加载旧 `alert-rules/` 子目录文件（与权威根文件漂移）+ §6.2 `METRICS_ALLOWED_SOURCES` 默认网段 `172.30.0.0/16` 与实际内网 `172.22.0.0/16` 不匹配（生产抓取会 403）+ §6.3 旧 api 构建。另 §6.4 无 alertmanager、§4 Langfuse 未上报。
- **证据**：`evidence/prod-go-live/observability-engineer/OBSERVABILITY_REPORT.md` §1/§2/§3/§6 + `drill_rpo_alerts.json`（firing 与 clear 两次 `/api/v1/alerts` 原文）+ `prometheus.yml.orig.bak`。

### G9 生产密钥/库/备份独立性—— ✅ 实测（配置级独立 + 完整 prod 栈健康已证实）
- **判定**：`INDEPENDENCE.json` `checks.independent = true`：项目名 `after-sales-prod`（≠`after-sales-preview`）、库名 `after_sales_prod`（≠`langgraph`）、PG 密码指纹不同、数据卷 `./data/prod-*`（与 preview 不相交、不触碰 `./data/preview-*`）、备份目录 `./data/prod-backups{,-offsite}`、子网 `172.31.0.0/16`、宿主端口 `8080:80`/`8843:443`（与 preview 80/443 不相交）、nginx `prod.conf`（≠preview.conf）、密钥 `deploy/.env.production`（全独立随机，gitignore）。**迁移数据面**：全新 prod-like 库 `migrate_cli` exit 0（17 表、复合外键正确、RLS 生效，`MIGRATE_VERIFY.md`）。
- **完整 `after-sales-prod` 栈带起健康：✅ 已证实**（`PROD_STACK_HEALTH.md`，T7）：compose `migrate` exit 0 + api/worker/frontend/nginx/postgres/redis 全容器 **healthy**，nginx `8080`/`8843` 暴露，nginx `/healthz`(8080)=200、api `/api/healthz`(8843)=200（body=`{"status":"ok"}`）、frontend `/`(8843)=200、`/api/metrics`(公网)=404（内网化正确阻断）。三表存在 + 复合 FK 正确 + RLS `enabled+forced+policies=1` + `app_runtime`/`backup_role` 最小权限。
- **⚠️ 边界（如实）**：生产栈健康证实的是容器级带起；受信 TLS（G10）就绪前**不可对外暴露 8080/8843**（当前 443 用自签）。**生产栈运行时 RLS / 告警端到端复验**（DEPLOY_BASELINE §7#8）仍为**部署期执行项**——本机取证多用 preview + 一次性独立测试库（边界3 口径）。
- **证据**：`evidence/prod-go-live/deploy-engineer/INDEPENDENCE.json`、`DEPLOY_BASELINE.md` §3/§5/§6.2/§7#7、`PROD_STACK_HEALTH.md`、`MIGRATE_VERIFY.md`、`MIGRATE_FAILURE.log`。

### G10 受信 TLS—— ❌ BLOCKED-需外部
- **判定**：现有证书 `deploy/secrets/certs/server.crt|key` 为**自签**（Subject=Issuer=CN=preview.local，1 年）；**无受信 CA 证书链**。
- **证据**：`evidence/prod-go-live/deploy-engineer/DEPLOY_BASELINE.md` §4。
- **解锁条件**：正式生产**域名** + 受信 CA（公共/企业 CA）签发的 `fullchain`（含中间链）+ `privkey`，替换后经 `nginx -t` + `openssl s_client -connect host:8843` 复核链完整。

### G11 人工审批强制—— ✅ 实测
- **判定**：退款/退货/改址必经唯一 `human_approval` interrupt；无任何 `direct → execute_*` 边；执行节点 5 重复核（含审批状态必须 APPROVED）；批准须显式二次确认（`confirmation`）+ CAS 单抢占；拒绝→`REJECTED`/`HUMAN_HANDOFF` 绝不执行；`platform_admin` 非租户审批角色。跨租户审批/读取→404+越权留痕。
- **证据**：`evidence/prod-go-live/security-auditor/RLS_ISOLATION_REPORT.md` §三 + `tests/test_approval_idempotency.py`/`tests/test_execution_engine.py`/`tests/test_tenant_isolation.py`/`tests/test_launch_gate_audit.py`（60 passed）。

### G12 只读 + shadow 先于 live—— ✅ 有序
- **判定**：当前 `EXECUTION_MODE=shadow`、`EXECUTION_PROVIDER=mock`、`LAUNCH_GATE_STRICT=true`；`_run_shadow` 只生成待执行记录+模拟回执（`simulated=true`），**不调用任何真实资金接口**；未切 live。
- **证据**：`evidence/prod-go-live/observability-engineer/OBSERVABILITY_REPORT.md` §4、`acceptance-engineer/LLM_GATEWAY_ACCEPTANCE.md` §4、`deploy-engineer/DEPLOY_BASELINE.md` §6.1；`SHADOW_TO_LIVE_GATE.md`（S0–S6 分步门控）。本任务**不执行 live 切换**。

### G13 首批真实租户书面确认—— ❌ BLOCKED-需业务方
- **判定**：`LAUNCH_ALLOWED_TENANTS=__NONE_APPROVED_YET__`（fail-closed）、`AUTH_LOGIN_CREDENTIALS={}`（无真实租户哈希）；**无任何真实租户书面确认函**。当前 `TENANT-A/B` 为历史演示租户（新基线已禁用 `seed_default`）。
- **证据**：`evidence/prod-go-live/deploy-engineer/DEPLOY_BASELINE.md` §3.1；`CANARY_TENANT_CONFIRMATION_TEMPLATE.md`（模板）。
- **解锁条件**：真实租户（业务方）签署书面确认函 + `hash_login_credentials.py` 产出 argon2id/bcrypt PHC 注入 `AUTH_LOGIN_CREDENTIALS` + `LAUNCH_ALLOWED_TENANTS` 注入 + 成员/角色授予 + 审计留痕。

### G14 7 天连续观察—— ❌ 待切 live 后
- **判定**：当前未切 live，观察期未开始。
- **证据**：`evidence/prod-go-live/observability-engineer/OBSERVABILITY_REPORT.md` §5（7 天监控指标集/看板/任一资金异常立即关 live 转人工动作）；`SHADOW_TO_LIVE_GATE.md` S6。
- **所需输入**：切 live 后 ≥7 天连续观测记录 + 每日备份恢复演练记录 + 告警无异常。

### G15 对账可核实—— ⚠️ 部分
- **判定**：对账入口可达 + 外部未知/无 provider → `MISMATCHED`→`HUMAN_HANDOFF` + 审计留痕（`execution.reconcile.mismatch`、`execution.compensated`）——**✅**；但 **mismatch 计数后台未实测**（`reconcile_mismatch_total` 指标运行态未计量，旧构建）。**⚠️ 部分**。
- **证据**：`evidence/prod-go-live/test-runner/reconcile_evidence.json`（TENANT-A unknown→mismatched=3、human_handoff；TENANT-B 无 provider→2；audit 留痕）、`CONCURRENCY_RECOVERY_REPORT.md` §5；`deploy/records/COMPENSATION_RECONCILIATION_REPORT.md`。

---

## 三、整体裁决

### 结论：`NO-GO`（正式生产上线当前不可放行）／**有条件 GO 的"能力基础"已就绪，但受外部输入阻断**

**依据**：
- **能力/安全/合规/幂等/可回滚侧（Go 的能力基础）—— 已在数据面 + 沙箱充分验证（✅/⚠️部分）**：
  ① **✅ 已验证**：G1 发布基线（rc1→cd743d3，基线忠实）、G3 RLS 动态（边界3 真实 PG）、G4 并发幂等（边界1/2/3）、G7 恢复备份（边界3）、G9 生产独立性（配置级 independent=true + 完整 prod 栈健康**已证实**，PROD_STACK_HEALTH）、G11 人工审批、G12 shadow 先于 live。**无审批绕过、无跨租户、无重复执行、可对账（沙箱级）、具备回滚/暂停/人工接管机制**。
  ② **⚠️ 部分**：G2 镜像 digest（内容/配置确定性 ✅，**字节级冷构建未复现**＝部署期执行项）、G8 告警演练（RpoExceeded 真实闭环 ✅；其余需当前构建运行时复验）、G15 对账可核实（入口/转人工/留痕 ✅；mismatch 计数后台未实测）。
- **正式生产放行被外部输入阻断（❌/⚠️，均属边界4 真实外部依赖）**：
  | 门禁 | 状态 | 边界 | 责任人 | 解锁条件 |
  |------|:---:|:---:|------|------|
  | G10 受信 TLS | ❌ BLOCKED | 边界4 | deploy-engineer | 真实域名 + 受信 CA fullchain + `nginx -t` + `openssl s_client`复核 |
  | G13 首批真实租户书面确认 | ❌ BLOCKED | 边界4 | release-manager/业务方 | 业务方签署确认函 + PHC 注入 + 白名单/凭据 + 成员/角色授予 |
  | G14 7 天观察 | ❌ 待切 live 后 | 边界4 | observability-engineer | 切 live 后 ≥7 天连续观测 |
  | G5 真实权重模型评测 | ❌ BLOCKED | 边界4 | acceptance-engineer | 真实自托管权重端点 + `write_op_pass=true` 报告 |
  | G6 真实资金链路 | ⚠️ 部分 | 边界2/边界4 | deploy-engineer+acceptance-engineer | 生产网关沙箱 + `EXECUTION_PROVIDER=sandbox_http` + 端到端复跑 |

**判断**：系统在"能力/安全/合规/幂等/可回滚"层面已获充分验证，构成 **GO 的能力基础**；但正式生产上线（对真实租户切 live/放量）受 **G10（受信 TLS）+ G13（真实租户书面确认）+ G14（7 天观察）+ G5（真实权重模型）+ G6（真实资金链路）** 这 5 项**边界4 真实外部依赖**阻断，故 **当前 NO-GO**。一旦上述外部输入到位，且"生产栈运行时 RLS/告警复验 + `git archive` clean-context 字节级重建"（部署期执行项）通过，可转 **有条件 GO**。

### 有条件 GO 的触发条件（全部满足后转 GO，并按此复核）
1. **G10 受信 TLS**：生产域名 + 受信 CA 证书链替换自签，`nginx -t` + 端到端 HTTPS 复验。
2. **G13 真实租户书面确认**：真实租户（业务方）签署确认函，`AUTH_LOGIN_CREDENTIALS`（argon2id）+ `LAUNCH_ALLOWED_TENANTS` 注入 + 成员/角色授予 + 审计。
3. **G5 真实权重模型**：先修覆写证据的测试 → 部署真实自托管端点 → 对 `self-hosted-model` 评测产出全量 24 用例 `write_op_pass=true` → `HIGH_CONFIDENCE_MODELS` 显式列出该 id。
4. **G6 真实资金链路**：部署生产网关沙箱（或真实资金渠道联测），`EXECUTION_PROVIDER=live/sandbox_http` + fail-closed。
5. **G14 7 天观察**：切 live 后连续 7 天观测达标。
6. **G2/G8/G9 部署期复验（切 live 前必办）**：`git archive release/v1.0.0-rc2` clean-context 字节级重建并刷新 digest（需 Docker engine 可连接）；`METRICS_ALLOWED_SOURCES` 网段修正；alert-rules 权威文件对齐；当前构建重建对账/指标；生产栈运行时 RLS/告警端到端复验；注入 Langfuse 密钥验证 trace 落地；**`pre_deploy_checks.sh` 在具备 docker+bash+PG 的目标服务器补全**（本沙箱无 docker/bash，静态=仓库不干净→FAIL、compose/secret→CANNOT RUN、总体 BLOCKED，**未宣称已通过**）。
7. **放量审批**：`SCALEUP_APPROVAL-<ts>.md` 目标租户 `admin`/`approver` 二次确认。

### 放量红线（任一触发即关 live 转人工并评估回滚）
未审批即执行 / 同一 operation_id 重复执行 / 跨租户访问被放行 / 对账 mismatch 且不可核实 / 告警命中资金红线（CrossTenantAccessDetected、RpoExceeded、RtoExceeded、ApiHighErrorRate、ApprovalFailureRateHigh、HighHumanInterventionRate、ReconciliationMismatch）/ 每日恢复演练失败。

---

## 四、tag / 镜像 digest / 迁移版本一致性（本次 rc2 口径）

> 项目**不设数字 schema 版本号**（业务 schema 全为 `CREATE TABLE IF NOT EXISTS` 幂等建表）；`checkpoint_migrations` 表 version 由官方 `langgraph-checkpoint-postgres==3.1.2` 迁移机制维护（非项目代码）；`deploy_config_version="0.1.0"` 为部署配置版本（`src/config.py`，非 schema）。因此"一致性"以**4 点**表述：

1. **迁移定义 commit**：`696444a`（`shipping_events` 复合外键修复）为基线 commit（`cd743d3`）祖先；rc2 基线 commit 链含之。
2. **镜像 digest 来源**：api `after-sales-prod-api@sha256:5d39f030...`、frontend `after-sales-prod-frontend@sha256:12c35ff7...` 均**自基线 commit 构建、含迁移修复**（当前为工作树构建，字节级 clean-context 重建=部署期执行项）。
3. **全新库 clean migrate**：`MIGRATE_VERIFY.md` 全新 prod-like 库 `migrate_cli` exit 0（17 表、复合 FK 正确、RLS FORCE 生效、无 InvalidForeignKey）。
4. **checkpoint 版本驱动**：`langgraph-checkpoint-postgres==3.1.2`（`requirements-lock.txt` 钉定），官方 `checkpoint_migrations` 表结构由该依赖迁移机制维护；`deploy_config_version`（`src/config.py`）== `DEPLOY_CONFIG_VERSION`（`deploy/.env.production`）== compose 默认 `${DEPLOY_CONFIG_VERSION:-0.1.0}`（三处一致）。

> **统一不变量**：`tag.commit（release/v1.0.0-rc2）== build_ref == migrations_schema_source == config_version_source`。`IMAGE_DIGESTS.json` 的 `release_baseline.tag/commit` 字段必须与 tag 精确一致（本次 rc2 在单个 commit 建 annotated tag）。**校验动作**（放量前）：`git rev-parse release/v1.0.0-rc2^{commit}` == 记录值；该 commit 的 `migrations.py` 构成镜像内 schema；digest 与 tag 一一对应；全新库 migrate exit 0 + 全容器 healthy 配套记录。【Docker engine 当前不可连接（非提升 token），clean-context 字节级重建及 digest 刷新列为部署期执行项 / BLOCKED-需 engine 可连接（提升完整 Admin token 或加入 docker-users 组）。】

---

## 五、签名
- 产出行：`release-manager`（t6）· **定稿**
- 说明：本清单基于 t1–t5 各角色 `evidence/prod-go-live/<role>/` 真实证据定稿。所有 ✅ 均可溯源；所有 ❌ BLOCKED 均给出所需输入；凡"完整 prod 栈带起健康 / 运行时告警复验 / 对账指标重建"未实际执行者，如实标注为部署期执行项。**本清单不虚报达标、不因外部输入缺失而搁置最终裁决。**
