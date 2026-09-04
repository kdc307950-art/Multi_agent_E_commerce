# DELIVERABLES_SUMMARY — 四类交付物汇总与放量审批建议

> **任务**：t9 · 汇总四类交付物与放量审批建议（安全合规评审员 / security）
> **对照**：团队上线目标"产出 Canary 租户确认函 / 每日观察记录 / 业务指标报告 / Shadow 到 Live 评审记录"
> **汇总基准**：`evidence/prod-go-live/release-manager/GO_NO_GO.md`（**定稿 NO-GO**）+ `deploy/records/*` 实际文件
> **日期**：2026-09-04
> **总判定**：四类交付物**均已形成产物**，但**全部或部分处于"模板/框架已就绪 + 关键证据待填"状态**，核心被 **5 项边界4 外部依赖**阻塞：
> `真实租户书面确认(G13)`、`正式域名+受信CA(G10)`、`真实自托管LLM写模型评测(G5)`、`真实网关/资金链路(G6)`、`7天连续观察(G14)`。
> **放量建议：当前 NO-GO（有条件 GO 的能力基础已就绪），不建议放量，直至 5 项外部依赖解锁 + 部署期复验通过 + 同租户 admin/approver 二次确认。**

---

## 0. 诚实声明（实测 vs 待验证）

| 边界 | 说明 | 本汇总涉及内容 |
|------|------|--------------|
| **实测（已产出可追溯值）** | 真实运行/演练产物，可复核 | 指标汇总（演练口径）、部署记录、安全动态验收、DR 演练、shadow 沙箱验收、生产栈容器健康 |
| **模板/框架（结构就绪，待填）** | 字段齐全但关键值为 `<...>` / ⬜ | Canary 确认函、7 天观察记录、Shadow→Live 评审记录 |
| **待验证/待外部输入** | 需业务方/用户/部署提供 | 真实租户名单、受信 CA、真实 LLM 权重端点评测、真实网关沙箱、7 天观察期 |

> **红线**：本汇总**不把模板/未实测项虚报为达标**；凡 `BLOCKED-需外部` / 待验证，一律如实标注，并把证据边界（单元 / mock 沙箱 / 真实 PG 测试库 / 边界4 真实外部依赖）写清。

---

## 1. 四类交付物 → 证据文件路径 + 状态

### 1.1 Canary 租户确认函
| 项 | 内容 |
|---|------|
| **主文件** | `deploy/records/CANARY_TENANTS-20260904-212814.md`（最新，t2；状态=`[AWAITING_USER_CONFIRMATION]`） |
| 历史版本 | `deploy/records/CANARY_TENANTS-20260904-030903.md` |
| 模板 | `deploy/records/CANARY_TENANTS-TEMPLATE.md`、`evidence/prod-go-live/release-manager/CANARY_TENANT_CONFIRMATION_TEMPLATE.md` |
| 数据源 | `deploy/records/tenants.json`（**示例**租户 ACME-RETAIL / GLOBEX-ECOM，已过 `create_bootstrapped_tenants.py --dry-run`） |
| **状态** | **待用户外部输入**：真实租户名单/成员/角色未提供；`LAUNCH_ALLOWED_TENANTS=__NONE_APPROVED_YET__`、`AUTH_LOGIN_CREDENTIALS={}`、`AUTH_CREDENTIAL_HASH=argon2id`。示例租户**不得**放量。 |
| 对应门禁 | `GO_NO_GO.md` **G13 ❌ BLOCKED-需业务方** |

### 1.2 每日观察记录
| 项 | 内容 |
|---|------|
| **主文件（模板）** | `deploy/records/7DAY_ACCEPTANCE_TRACKER.md`（**模板**，Day1–Day7 均为 ⬜，无 `7DAY_ACCEPTANCE_TRACKER-<dates>.md` 实填文件） |
| 机制/数据源 | `deploy/observability/DAILY_CHECKS_RUNBOOK.md`（8 项检查/阈值/处置）、`deploy/observability/OBSERVABILITY_VERIFICATION.md`、`GET /api/metrics`、`GET /api/audit`、`deploy/drills/record_template.md` |
| **状态** | **待切 live 后观察**：当前未切 live（`EXECUTION_MODE=shadow`），观察期未开始；需 ≥7 天连续观测 + 每日备份恢复演练记录。 |
| 对应门禁 | `GO_NO_GO.md` **G14 ❌ 待切 live 后** |

### 1.3 业务指标报告
| 项 | 内容 |
|---|------|
| **主文件** | `deploy/records/METRICS_SUMMARY-20260904-030903.md`（**正式交付**，回填实测值 + 如实标注未实测） |
| 模板 | `deploy/observability/BUSINESS_METRICS_REPORT-TEMPLATE.md` |
| **状态** | **部分实测**：RTO=1.668s（Stopwatch 实测）、审批耗时 0.145s/0.204s（实测）、RPO=900s（**周期界定**达标，15min pg_dump 周期）、错误率/人工介入率=0（**本演练口径** 7/7 PASS）、无跨租户/无重复执行/无审批绕过/无未审计写 = PASS（实测）。**生产运行指标待补强**：`api_requests_total` 缺 `status`；无 `approval_decision_latency_*`/`reconcile_mismatch_total`/`human_intervention_total`/`security_denials_total`/`drill_{rpo,rto}_seconds`；对账 mismatch 计数**未实测**（后台）。 |
| 对应门禁 | `GO_NO_GO.md` **G2/G8/G15 ⚠️ 部分**（指标/告警/对账增强） |

### 1.4 Shadow → Live 评审记录
| 项 | 内容 |
|---|------|
| **主文件（模板）** | `deploy/records/SHADOW_TO_LIVE_REVIEW.md`（**模板**，§1/§2/§3 全 ⬜；无 `SHADOW_TO_LIVE_REVIEW-<租户ID>-<ts>.md` 实填文件） |
| 支撑/已回填 | `deploy/records/SCALEUP_APPROVAL-20260904-030903.md`（**正式交付**，结论=**暂缓放量** + 待办清单）、`evidence/prod-go-live/release-manager/SHADOW_TO_LIVE_GATE.md`（S0–S6 门控）、`GO_NO_GO.md`（定稿 NO-GO）、`FINAL_ACCEPTANCE.md`、`ROLLBACK_PAUSE_TAKEOVER.md` |
| **状态** | **待 shadow 期数据 + 外部输入**：7 天观察未开始、真实租户/受信 CA/真实 LLM/真实网关未到位；`SCALEUP_APPROVAL` 已给出"暂缓放量（附待办清单）"。 |
| 对应门禁 | `GO_NO_GO.md` **G5/G6/G10/G13/G14**（第 3 项 live 切换前置） |

---

## 2. 交叉校验矩阵（deliverable ↔ evidence ↔ 状态）

| 交付物 | 权威证据文件 | 证据状态 | 已回填实测值 | 阻塞项 | 结论 |
|--------|-------------|:---:|:---:|--------|------|
| Canary 确认函 | `CANARY_TENANTS-20260904-212814.md` | 模板+示例 | 否（示例租户） | 真实租户名单/成员/角色 | **待用户确认**（G13） |
| 每日观察记录 | `7DAY_ACCEPTANCE_TRACKER.md` | 模板 | 否 | 7 天观察期 | **待切 live 后**（G14） |
| 业务指标报告 | `METRICS_SUMMARY-20260904-030903.md` | **正式交付** | 是（演练口径） | 生产运行指标增强 | **部分实测**（G2/G8/G15） |
| Shadow→Live 评审 | `SHADOW_TO_LIVE_REVIEW.md` + `SCALEUP_APPROVAL-030903.md` | 模板+审批单 | 审批单已回填（暂缓） | 5 项外部依赖 | **待外部输入**（G5/G6/G10/G13/G14） |

> **一致性核对**：四类交付物指向的 evidence 文件均真实存在（`CANARY_TENANTS-*`、`7DAY_ACCEPTANCE_TRACKER.md`、`METRICS_SUMMARY-*`、`SHADOW_TO_LIVE_REVIEW.md`、`SCALEUP_APPROVAL-*`、`GO_NO_GO.md`），与 `GO_NO_GO.md` 定稿（5 项边界4 外部依赖阻断 → NO-GO）口径一致，**无相互矛盾**。`SCALEUP_APPROVAL-20260904-030903.md` 是针对 **preview 环境（TENANT-A/B）** 的放量审批，与面向生产栈 `after-sales-prod` 的 `PRODUCTION_DEPLOYMENT_record-20260904-2055.md`（t6/t7/t8）应**区分口径**：生产栈为"堆栈健康+全新库迁移+隔离校验"，而放量审批/7 天观察针对**生产真实租户**尚未满足。

---

## 3. 阻塞项（待用户/外部输入）— 明确归类

| # | 外部输入 | 阻塞的交付物 | 责任方 | 解锁条件 | 证据/门禁 |
|---|---------|-------------|--------|---------|-----------|
| 1 | **真实租户名单 + 成员/角色** | Canary 确认函、Shadow→Live 评审 | release-manager / 业务方 / 用户 | 业务方签署书面确认函 → `hash_login_credentials.py`(argon2id) 注入 `AUTH_LOGIN_CREDENTIALS` + `LAUNCH_ALLOWED_TENANTS` 注入 + 成员/角色授予 + 审计留痕 | **G13 ❌ BLOCKED** |
| 2 | **正式域名 + 受信 CA** | Shadow→Live 评审（对外暴露前提） | deploy-engineer | 真实域名 + 受信 CA fullchain（含中间链）+ `nginx -t` + `openssl s_client` 复核 | **G10 ❌ BLOCKED** |
| 3 | **真实自托管 LLM 权重端点 + 写评测** | 业务指标报告（真实模型）、Shadow→Live 评审 | acceptance-engineer | 部署自托管权重端点 → `evaluate_models.py` 产出 `write_op_pass=true` → `HIGH_CONFIDENCE_MODELS` 显式列出；`LLM_BACKEND=openai_compatible` | **G5 ❌ BLOCKED** |
| 4 | **真实网关沙箱 / 资金链路** | Shadow→Live 评审（live 切换） | deploy-engineer + acceptance-engineer | 部署内网 `sandbox_gateway` → `GATEWAY_BASE_URL`/`GATEWAY_API_KEY` → `EXECUTION_PROVIDER=sandbox_http` → 端到端（shadow-only 提交） | **G6 ⚠️ 部分** |
| 5 | **7 天连续观察** | 每日观察记录、Shadow→Live 评审 | observability-engineer | 切 live 后 ≥7 天连续观测 + 每日备份恢复演练 + 告警无异常 | **G14 ❌ 待切 live 后** |

> **说明**：输入 1（真实租户）与输入 2（受信 CA）是**生产可对外**的最基础前置；输入 3/4（真实模型/资金）决定"写路径"与"真实执行"能力；输入 5（7 天观察）是 Shadow→Live 切换的最终门禁。

---

## 4. 剩余待办清单

### 4.1 外部输入（需用户/业务方/部署提供）
- [ ] **G13** 真实租户名单 + 成员/角色书面确认 → 生成已签署 Canary 确认函
- [ ] **G10** 正式域名 + 受信 CA 证书链（替换自签）→ `nginx -t` + 端到端 HTTPS 复核
- [ ] **G5** 真实自托管 LLM 权重端点 + `write_op_pass=true` 评测报告 → 写模型白名单
- [ ] **G6** 生产网关沙箱/真实资金链路 + `EXECUTION_PROVIDER=sandbox_http` + 端到端
- [ ] **G14** 切 live 后 ≥7 天连续观察 + 每日演练记录

### 4.2 工程/观测补强（内部，部署前/放量前必办）
- [ ] **指标增强**：`src/observability/metrics.py`+`src/api/routes.py` 增加有界标签（`api_requests_total` 加 `status`；新增 `approval_decision_latency_seconds`、`reconcile_mismatch_total`、`human_intervention_total`、`security_denials_total`、`drill_{rpo,rto}_seconds`）；禁用高基数 `tenant_id` 标签
- [ ] **告警修复**：①`alert-rules.yml` 权威文件与运行态 `alert-rules/` 子目录对齐；②`METRICS_ALLOWED_SOURCES` 网段（`172.30.0.0/16` vs 实际内网 `172.22.0.0/16`）修正；③重建当前构建 api/worker 使新标签生效；④补 alertmanager 配置
- [ ] **对账/指标实测**：后台 `reconcile_tenant` mismatch 计数实测；生产栈运行时 RLS/告警端到端复验
- [ ] **字节级重建**：`git archive release/v1.0.0-rc2` clean-context 重建并刷新 `IMAGE_DIGESTS.json`（需 Docker engine 可连接，当前**不可连接**）
- [ ] **Langfuse trace 落地**：注入 `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`，验证 `tenant_id/session_id/environment` 真实上报
- [ ] **生产栈数据面复验**：生产 `after-sales-prod` 运行时 RLS 动态/并发幂等复验（当前取证多为 preview + 一次性测试库）

### 4.3 评审/签发
- [ ] **Shadow→Live 评审**：前置 1–10 全满足 + 业务方书面确认后签发（`APPROVE` / 否则 `HOLD`）
- [ ] **放量审批单**：`SCALEUP_APPROVAL-<ts>.md` 目标租户 `admin`/`approver` 二次确认 + 审计留痕（**同租户**；`platform_admin` 不适用）

---

## 5. 放量审批建议（参考 `SCALEUP_APPROVAL-TEMPLATE.md` + `GO_NO_GO.md`）

### 5.1 当前裁决
> **结论：`NO-GO`（正式生产放量当前不可行）／ 有条件 GO 的"能力/安全/合规/幂等/可回滚"基础已就绪。**

依据 `GO_NO_GO.md` 整体裁决：能力侧（G1 发布基线、G3 RLS 动态、G4 并发幂等、G7 恢复备份、G9 生产独立性、G11 人工审批、G12 shadow 先于 live）已充分验证；但 **G10（受信 TLS）+ G13（真实租户确认）+ G14（7 天观察）+ G5（真实权重模型）+ G6（真实资金链路）** 这 5 项**边界4 真实外部依赖**未到位，故放量须暂停。

### 5.2 有条件 GO 的触发条件（全部满足后转 GO）
1. **G10 受信 TLS**：生产域名 + 受信 CA 替换自签。
2. **G13 真实租户确认**：业务方签署确认函 + `AUTH_LOGIN_CREDENTIALS`(argon2id) + `LAUNCH_ALLOWED_TENANTS` + 成员/角色 + 审计。
3. **G5 真实权重模型**：部署真实端点 → `write_op_pass=true` → `HIGH_CONFIDENCE_MODELS`。
4. **G6 真实资金链路**：生产网关沙箱/真实渠道，`EXECUTION_PROVIDER=sandbox_http`（shadow-only 提交）。
5. **G14 7 天观察**：切 live 后连续 7 天达标。
6. **部署期复验（切 live 前必办）**：`git archive` clean-context 字节级重建 + 刷新 digest；`METRICS_ALLOWED_SOURCES` 网段修正；alert-rules 对齐；重建对账/指标；生产栈运行时 RLS/告警端到端复验；注入 Langfuse 验证 trace。
7. **放量审批**：`SCALEUP_APPROVAL-<ts>.md` 目标租户 `admin`/`approver` 二次确认 + 审计留痕。

### 5.3 建议的放量阶梯（渐进）
| 阶梯 | 范围 | 前置 | 放量动作 | 观察/复盘 |
|------|------|------|---------|-----------|
| Tier 0 | 首个真实租户（最小面） | G10+G13 就绪 | 仅放行 1 租户 | `healthcheck` 全过 + DR 可复核 + RPO≤15min/RTO≤60min |
| Tier 1 | 首 + 第 2 租户 | 上线阈值 + 无重复副作用 | 扩到 2 租户 | 指标正常 |
| Tier 2 | 目标区间 50% | 告警无异常 + 人工介入/错误率达标 | 逐步扩量 | — |
| Tier 3 | 全部目标租户 | 稳定期观察 | 全量 | 二次审批 |

> **放量红线（任一触发即关 live 转人工 + 评估回滚）**：未审批即执行 / 同 operation_id 重复执行 / 跨租户访问被放行 / 对账 mismatch 且不可核实 / 资金相关红线告警命中 / 每日恢复演练失败。**审批必须同租户**（`admin`/`approver`），跨租户拒绝并留痕；`platform_admin` 不作租户审批角色（`AGENTS.md` 宪法）。

### 5.4 审批意见（`SCALEUP_APPROVAL` 建议文本，供审批方填）
> **结论：暂缓放量（附待办清单）**。核心条件已达成（RPO 周期界定、RTO、仅 80/443、安全四项、D3–D6、FOUND-SOFTWARE-1 修复、观察链路）；**未实测/待外部项**（真实租户名单、受信 CA、真实 LLM 评测、真实网关、7 天观察、对账 mismatch、多租户并发、应用侧 trace、告警指标增强）列作放量前补强/放量后观察项。若团队接受将未实测项列为放量后观察项，可从 **Tier 0（首个真实租户）** 开始，仍须同租户 `admin`/`approver` 二次确认并留痕。

---

## 6. 结论

- **四类交付物均已有产物**，但状态分层：**业务指标报告 = 已回填实测值（演练口径）**；**Canary 确认函 / 每日观察记录 / Shadow→Live 评审 = 模板/框架就绪、关键证据待填**。
- **核心阻塞**为 5 项**边界4 真实外部依赖**（真实租户确认 G13、正式域名+受信 CA G10、真实 LLM 写评测 G5、真实网关/资金 G6、7 天观察 G14）。这些未解锁前，**不得对真实租户放量/切 live**。
- **放量建议**：当前 **NO-GO**；具备"有条件 GO"的能力基础。在 5 项外部依赖解锁 + 部署期复验（G2/G8/G9）通过 + 同租户 `admin`/`approver` 二次确认后，转 **有条件 GO**，从 Tier 0 渐进放量。

— *security（安全合规评审员）* · 2026-09-04
