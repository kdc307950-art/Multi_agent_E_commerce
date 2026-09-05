# RC3 生产就绪主报告（RC3 → 首批真实租户受控放量 · 就绪评估与证据缺口）

> **产出**：release-manager（发布经理）· prod-go-live 团队 · 任务 t11（队长汇总）
> **角色**：release-manager / deploy-engineer / security-auditor / qa-runner
> **事实来源**：本地 git + 文件实测（唯一事实来源）；`evidence/prod-go-live/<role>/` 各角色交付；`evidence/prod-go-live/test-runner/pytest_full_rerun.log`（本次权威全量 pytest 归档）。
> **诚实原则**：凡真实外部输入（受信 CA / 真实模型权重端点 / 真实资金网关 / 书面确认真实租户 / 目标服务器 / 7 天观察）未到位，一律如实标注 `❌ BLOCKED-需外部`；凡未实际执行（生产栈运行时复验 / clean-context 字节级重建 / 告警演练 / 对账计数）一律标为部署期执行项。**本报告不虚报任何达标。**

---

## 一、一句结论

系统已建成**生产候选基线**：阶段 0（版本冻结 + 发布清单 + 密钥状态审计）与阶段 1（发布证据一致性统一至 RC3 口径 + 全量 pytest 可溯源归档）**已闭环**；但**正式生产放量当前为 `NO-GO`**——受 5 项**真实外部依赖（证据边界 4）**阻断（受信 TLS、真实自托管模型、真实资金网关、首批书面确认真实租户、7 天观察）。能力/安全/幂等/可回滚侧（边界 1/2/3）已充分验证，构成 **GO 的能力基础**；外部输入到位 + 部署期执行项完成后方可转「有条件 GO」。

---

## 二、真实基线锁定（git 实测，唯一事实来源）

| 项 | 值 |
|---|---|
| 发布标签（当前发布锚点） | `release/v1.0.0-rc3` = annotated tag 对象 `d1d2867` → commit `3ccab5c` == `HEAD` |
| 代码 commit（tag/evidence/release 收口 commit） | `3ccab5ca8e59bce967c0ce986fd97bda5d7ff6df`（== `HEAD` == `main`，本次为 docs/tests/gitignore-only 收口） |
| 运行面（镜像代码）基线 | `cde30fb6e95ed8c9f6d5559cb15ec674fe2d0c09`（src/deploy 运行面与 rc2 相同，rc3 未改动运行面） |
| 镜像 digest | api `after-sales-prod-api@sha256:5d39f030...`；frontend `after-sales-prod-frontend@sha256:12c35ff7...`（**工作树构建、非字节级复现；rc3 未重建**；clean-context 重建=部署期执行项） |
| 迁移版本 | **无数字 schema**；迁移定义 `696444a`（shipping_events 复合外键修复）；全新库 clean migrate exit 0（见 `MIGRATE_VERIFY.md`）；checkpoint 由 `langgraph-checkpoint-postgres==3.1.2` 钉定驱动 |
| 配置版本 | `deploy_config_version=0.1.0`（`src/config.py` / `deploy/.env.production` / compose 默认，三处一致） |
| 测试报告 | **`390 passed, 34 skipped`（424 collected，**53.83s**，EXIT=0，发布基线环境实测，见 `pytest_full_rerun.log`）**。34 skipped = 33 项 PostgreSQL（缺 `DATABASE_URL`）+ 1 项 CrewAI（`test_hardening_acceptance.py:384` 未装 crewai）。 |
| 回滚版本 | `release/v1.0.0-rc2`（历史锚点，不可移动）＋`rollback.sh`/`restore_drill.sh`/`OPS_RUNBOOK` 暂停/人工接管机制（DR 加密恢复演练 RTO=0.643s / RPO≤900s 上界） |
| 分支/git 状态 | 当前分支 `codex/prod-readiness`（自 rc3 创建，main 未动）；**基线时刻 rc3 工作区 clean**；当前 working tree 因团队发布证据编辑非 clean |
| 关键一致性 | `tag.commit(3ccab5c)` / 运行面基线 `cde30fb` / 迁移 `696444a` / config `0.1.0` **四者有意分离**，放量校验须分别核对（不再假设相等） |

历史锚点（不可移动，仅历史留存）：`release/v1.0.0-rc1` = tag 对象 `2120c4b` → commit `cd743d3`；`release/v1.0.0-rc2` = tag 对象 `290b830` → commit `fa7c9a3`；`bca4861` 为 rc2 谱系早期「阶段一 rc2 基线收口」**父提交（祖先），非 rc2 锚**。

---

## 三、统一口径（本次放量前必守）

1. **发布锚**：`release/v1.0.0-rc3` 以基线尖端（`3ccab5c`）为准；rc1/rc2 为不可移动历史锚点。
2. **镜像来源**：当前镜像为**工作树构建产物**（代码等于运行面基线 `cde30fb`），**非 RC3 clean-context 字节级重建产物**；RC3 **未重建**。要达到字节级可复现，须在具备 Docker 引擎访问的环境以 `git archive release/v1.0.0-rc3 | tar -x` + `compose build --no-cache --provenance=false --sbom=false` 重建并刷新 digest（部署期执行项）。
3. **测试口径**：`390 passed, 34 skipped, 53.83s, EXIT=0`（发布基线环境实测，见 `pytest_full_rerun.log`）；旧叙述性 `54.63s` 已全部替换；`320 passed / 70 errors` 为历史沙箱 `tmp_path` PermissionError（WinError 5）环境问题（70 个 error 全为 ERROR at setup，0 真失败），仅作历史说明保留。
4. **容器健康**：`PROD_STACK_HEALTH.md` 与各引用文档限定为「**一次 bring-up 观测（T7，2026-09-04），当时全 healthy，非当前运行态证明 / 未经本次复核**」。
5. **密钥**：`deploy/.env.production` 被 `.gitignore` 忽略、`git ls-files` 为空、`git log` 从未提交 → 不入 Git；运行期 `${VAR:?}` env-file 注入不 baked 入镜像；日志 `LLM_LOG_REDACT=true` 强凭据不落日志。关键占位均为 fail-closed：`AUTH_LOGIN_CREDENTIALS={}`、`LAUNCH_ALLOWED_TENANTS=__NONE_APPROVED_YET__`、`HIGH_CONFIDENCE_MODELS=__NO_VERIFIED_WRITE_MODEL__`、`EXECUTION_PROVIDER=mock`、`LLM_BACKEND=mock`。

---

## 四、阶段 0–7 就绪状态汇总

| 阶段 | 状态 | 说明 | 证据路径 |
|---|---|---|---|
| **0 冻结版本** | ✅ **已闭环** | 唯一发布版本锁定 rc3；生产修改禁令入 `codex/prod-readiness`；发布清单六要素成文；密钥状态审计（只读，未执行真实轮换） | `BASELINE_LOCK.md`、`RELEASE_CHECKLIST.md`、`security-auditor/KEY_STATE_AUDIT.md` |
| **1 发布证据一致性** | ✅ **已闭环** | 全部发布文档统一至 rc3 口径；修复 rc2 锚定偏差；残留 `320/70` 清除；`390/34` 由真实日志可溯源；观测时点限定 | 本报告 §3 + `GO_NO_GO.md` / `FINAL_ACCEPTANCE.md` / `DEPLOY_BASELINE.md` / `BASELINE_COMMIT_SCOPE.md` / `IMAGE_DIGESTS.json` / `README.md` / `PROJECT_STATUS.md`（均 rc3 口径） |
| **2 目标服务器部署生产栈** | ❌ **BLOCKED-需外部** | 生产目标=多节点 K8s/LB+PG 主备+HA；本会话交付物为单节点本机 compose；目标服务器=外部资产未接入；Docker 不在 PATH / 无法连接运行态 | `docker-compose.prod.yml`（单节点拓扑）、`INFRA_READINESS_GAP.md` |
| **3 PostgreSQL/RLS 生产验证** | ⚠️ **部分（边界3 已证，生产专栈复验=部署期）** | 功能在边界3（preview+一次性独立测试库）验证 ✅（RLS B1–B5 全拦、并发 N=256、DR 加密恢复、全新库 clean migrate exit 0）；**生产专栈 `after-sales-prod` 运行时复验（RLS/并发/恢复/告警）=部署期执行项** | `MIGRATE_VERIFY.md`、`security-auditor/RLS_ISOLATION_REPORT.md`、`rls_dynamic_probe.json`、`test-runner/DR_encrypted_restore.md` |
| **4 真实自托管模型** | ❌ **BLOCKED-需外部（0%）** | 现 `LLM_BACKEND=mock`（规则引擎）；`127.0.0.1:8001` 连接拒绝；`self_hosted_server.py` 复用 MockLLM；`llm_candidate_eval.json` 仅 stub（被测试覆写）；`HIGH_CONFIDENCE_MODELS` 空 → 写全 fail-closed 转人工 | `security-auditor/MODEL_TENANT_READINESS_GAP.md`、`evidence/llm_candidate_eval.json`、`acceptance-engineer/LLM_GATEWAY_ACCEPTANCE.md` |
| **5 真实 Sandbox Gateway** | ❌ **BLOCKED-需外部** | `sandbox_gateway` 代码/测试 ✅ PASS（SQLite 幂等、回调验签、compensate、N=256、`test_sandbox_e2e_flow`）；但 prod compose 无 `sandbox-gateway` 服务、`EXECUTION_PROVIDER=mock`、无 `GATEWAY_BASE_URL`/`GATEWAY_API_KEY`；当前 shadow 仅模拟回执不触真实资金 | `INFRA_READINESS_GAP.md`、`KEY_STATE_AUDIT.md` |
| **6 首批真实租户 Shadow** | ❌ **BLOCKED-需外部（0%）** | `LAUNCH_ALLOWED_TENANTS=__NONE_APPROVED_YET__`、`AUTH_LOGIN_CREDENTIALS={}`；无任何已签确认函；tenants.json 为示例 | `MODEL_TENANT_READINESS_GAP.md`、`CANARY_TENANT_CONFIRMATION_TEMPLATE.md` |
| **7 真实资金 Live 灰度** | ❌ **BLOCKED-需外部** | 受信 CA（自签）、真实模型、真实网关、首批真实租户、业务确认（S4）、7 天观察（G14）、观测链路（Langfuse trace=NO-OP）全部未就绪；当前处 S1 shadow fail-closed 安全默认态 | `INFRA_READINESS_GAP.md`、`observability-engineer/OBSERVABILITY_REPORT.md` |

**放量红线（任一触发即关 live 转人工并评估回滚）**：未审批即执行 / 同一 `operation_id` 重复执行 / 跨租户访问成功 / 对账 mismatch 且不可核 / 告警资金红线 / 每日恢复演练失败。

---

## 五、阶段 1 交付清单（本团队本次实际改动/新增）

**修改（rc3 口径统一，均 `M`）**：`README.md`、`PROJECT_STATUS.md`、`evidence/prod-go-live/release-manager/GO_NO_GO.md`、`FINAL_ACCEPTANCE.md`、`evidence/prod-go-live/deploy-engineer/DEPLOY_BASELINE.md`、`BASELINE_COMMIT_SCOPE.md`、`IMAGE_DIGESTS.json`、`PROD_STACK_HEALTH.md`。

**新增（`??`）**：`release-manager/BASELINE_LOCK.md`、`release-manager/RELEASE_CHECKLIST.md`、`security-auditor/KEY_STATE_AUDIT.md`、`security-auditor/RELEASE_DOC_CONSISTENCY_AUDIT.md`、`security-auditor/MODEL_TENANT_READINESS_GAP.md`、`test-runner/TEST_EVIDENCE_SOURCING.md`、`test-runner/DOC_VERIFICATION.md`、`deploy-engineer/INFRA_READINESS_GAP.md`、`test-runner/pytest_full_rerun.log`（本次权威全量 pytest 归档）。

**未改动**：任何 `src/**` 源码、任何 tag（rc1=`2120c4b`、rc2=`290b830`、rc3=`d1d2867`）、`main` 分支、Docker 映像。

> 注：`deploy/.env.production` 处于 `.gitignore` 且未被 git 跟踪，全程只读核验、未输出任何真实密钥值、未执行真实轮换。

---

## 六、诚实结论与放量前置条件

### 诚实结论
- **正式生产放量当前 `NO-GO`**（已有 `GO_NO_GO.md` 定稿支撑）。系统在能力/安全/合规/幂等/可回滚层面已获充分验证，构成 **GO 的能力基础**；但被 **5 项边界 4 真实外部依赖**阻断。
- 阶段 0/1 为本次实际闭环项；阶段 2–7 中，**目标服务器、真实模型、真实网关、受信 CA、真实租户、7 天观察均无法在本沙箱/本机环境由代码/Agent 闭环**，无一伪造。
- 当前为 `S1 shadow / fail-closed` 安全默认态：无真实租户放行、无写白名单模型、execution 不触真实资金。

### 转「有条件 GO」的前置条件（全部满足 + 部署期复验通过后）
1. **G10 受信 TLS**：真实域名 + 受信 CA fullchain 替换自签；`nginx -t` + 端到端 HTTPS 复核。
2. **G13 首批书面确认真实租户**：业务方签署确认函 + `argon2id` PHC 注入 `AUTH_LOGIN_CREDENTIALS` + `LAUNCH_ALLOWED_TENANTS` 注入 + 成员/角色授予 + 审计留痕。
3. **G5 真实权重模型评测**：部署真实自托管端点；修复覆写证据的测试；`evaluate_models.py --base-url <真实> --model-name self-hosted-model` 产全量 24 用例 `write_op_pass=true` 报告；`HIGH_CONFIDENCE_MODELS` 显式列出。
4. **G6 真实资金链路**：部署内网 `sandbox_gateway` + `EXECUTION_PROVIDER=sandbox_http/live` + 端到端复跑 + fail-closed。
5. **G14 7 天观察**：切 live 后 ≥7 天连续观测 + 每日恢复演练 + 告警无红线。
6. **部署期执行项（切 live 前必办）**：`git archive release/v1.0.0-rc3` clean-context 字节级重建并刷新 digest（需 Docker 引擎可达）；生产专栈运行时 RLS/告警端到端复验；`METRICS_ALLOWED_SOURCES` 网段修正；alert-rules 权威文件对齐；mismatch 计数后台实测；注入 Langfuse 密钥验证 trace 落地；`pre_deploy_checks.sh` 在具备 docker+bash+PG 的目标服务器补全（本沙箱无 docker/bash，静态 FAIL / compose/secret CANNOT RUN，**未宣称已通过**）。

---

## 七、最短时间表映射

| 时间 | 方案目标 | 本次状态 |
|---|---|---|
| 第 1 天 | 统一 RC3 文档、密钥轮换审计、发布冻结 | ✅ 已完成（阶段 0/1） |
| 第 2-3 天 | 目标服务器、TLS、生产 Compose | ❌ 需外部资产（目标服务器 + 受信 CA） |
| 第 4 天 | PG/RLS、checkpoint、备份恢复 | ⚠️ 边界3 已证；生产专栈复验=部署期 |
| 第 5-7 天 | 真实模型和评测 | ❌ 需外部（真实自托管端点） |
| 第 8 天 | Sandbox Gateway | ❌ 需外部（部署内网 sandbox_gateway） |
| 第 9 天 | 首批租户接入 | ❌ 需外部（业务方确认函 + PHC） |
| 第 10-16 天 | 7 天 Shadow | ❌ 待切 shadow 后观察 |
| 第 17 天 | 单租户、单业务类型 Live 灰度 | ❌ 全部前置到位后 |

**回滚方案就绪性**：✅ 机制层已验证（`rollback.sh`/`restore_drill.sh`/`OPS_RUNBOOK` §3/§4；DR 加密恢复演练 RTO/RPO 实测；`ROLLBACK_PAUSE_TAKEOVER.md`）。紧急回滚顺序：移租户白名单 → `EXECUTION_MODE=readonly` → 停 worker → 暂停 Nginx 写入口 → 保留 `operation_id`/审批/回调记录 → 备份 → 回滚 → 重跑健康/RLS/幂等测试 → 人工确认后恢复。**禁止删除操作记录或清空数据库。**

---

## 八、本沙箱/本机环境无法闭环的项（明确列示）

- **目标 Linux 服务器部署**：无外部资产接入；Docker 不在 PATH / 无法连接运行态 → 无法实测容器健康、TLS、公网暴露、防火墙。
- **真实自托管模型**：无真实权重端点；本地 `127.0.0.1:8001` 连接拒绝；无真实评测流量。
- **真实 Sandbox / Funds Gateway**：生产网关沙箱未部署；无 `GATEWAY_BASE_URL`/`GATEWAY_API_KEY`。
- **受信 CA 证书**：现有证书自签（`CN=preview.local`，subject==issuer）。
- **首批真实租户书面确认**：无业务方确认函、无 `argon2id` PHC、无真实 `tenant_id`。
- **7 天观察 / 每日恢复演练 / 对账签字**：需真实运行态与责任人每日执行。
- **clean-context 字节级镜像重建**：需 Docker 引擎可达（当前非提升 token 拒连 `npipe:////./pipe/dockerDesktopLinuxEngine`）。
- **生产栈运行时 RLS/告警端到端复验**：需生产栈运行态。

---

> **最终结论**：只有当真实租户、真实模型、真实网关、受信 TLS、PG/RLS 生产专栈复验、审批、对账、回滚和 7 天 Shadow **全部形成证据**后，项目才能从「生产候选」升级为「首批真实租户受控生产」。当前 = **生产候选（NO-GO）**。
