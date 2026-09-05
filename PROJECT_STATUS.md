# 项目状态说明（一页 · 面试作品 / RC5 生产候选 · 2026-09-05）

> **角色**：release-manager / prod-go-live 团队 · **任务**：t6 发布候选收口 · **交付**：`release/v1.0.0-rc3` 标签 + 重定稿 `GO_NO_GO.md` / `FINAL_ACCEPTANCE.md` + 生产候选版 `README.md`。
> **依据**：`evidence/prod-go-live/<role>/`（deploy-engineer/security-auditor/test-runner/observability-engineer/acceptance-engineer）真实证据；git 实况为唯一事实来源。

---

## 一、一句结论

系统定位为**面试级、单租户、低并发、人工审批、Shadow 的自托管作品**。当前仍为 RC5 生产候选、正式生产放量 `NO-GO`；**代表性自托管端点 + 真实 CrewAI 工具调用已闭环**，真实权重模型仍未验证。

## 二、发布基线与一致性

| 项 | 值 |
|---|---|
| 发布 tag | **`release/v1.0.0-rc5-candidate`** → `8d44e80`（当前候选；后续文档/代码改动尚未形成新 tag） |
| 基线 commit 链 | `7941246`(已测功能) → `3268a1c`(可复现构建；其间含 `ad168ef`/`8be2d97`/`03819a8`) → `696444a`(migrations 复合FK修复) → `8ddca48`(接受运行面) → `cd743d3`(BUSINESS_DATA_BACKEND, **rc1**) → `cde30fb`(T1/T7 发布证据+生产栈健康) → `59e37f2`(rc2 定稿历史) → `bca4861`(rc2 早期"阶段一 rc2 基线收口" **commit, 祖先；非 rc2 锚**) → `fa7c9a3`(**rc2 tag 目标 commit**) → **阶段一 rc3 收口提交(`3ccab5c`＝rc3 tag 目标＝HEAD)**。<br>注：`release/v1.0.0-rc3` = annotated tag 对象 `d1d2867` → commit `3ccab5c`(== HEAD)；`release/v1.0.0-rc2` = annotated tag 对象 `290b830` → commit `fa7c9a3`（rc2 文档收口尖端，历史、不可移动）；`bca4861` 仅为 rc2 谱系早期收口 commit 祖先，**不作 rc2 锚**。 |
| 镜像 digest | api `after-sales-prod-api@sha256:5d39f030...`；frontend `after-sales-prod-frontend@sha256:12c35ff7...`（**工作树构建观测值**；clean-context 字节级重建＝部署期执行项/BLOCKED-需 Docker engine 可连接） |
| 迁移版本一致 | 项目**无数字 schema 版本**。一致性＝①迁移定义 `696444a` ∈ 基线链 ②镜像含迁移修复 ③`MIGRATE_VERIFY` 全新 prod-like 库 clean migrate exit 0（17表/复合FK/RLS生效）④checkpoint 由 `langgraph-checkpoint-postgres==3.1.2` 钉定驱动；`deploy_config_version=0.1.0` 三处一致 |
| git 状态 | **基线时刻 clean**（阶段一已提交收口：测试默认写临时目录 + 真实 390/34 口径 + uv.lock 不入库，一次性提交；rc3 锚 `3ccab5c` 工作区**当时** clean）；HEAD=基线尖端（以 `release/v1.0.0-rc3` 为锚）。**当前 working tree 因团队发布证据编辑非 clean**（多文件 M/??，见 `git status`）——"clean"仅指基线提交时刻，不代表当前。 |

## 三、四种证据边界（本发布统一口径）

| 边界 | 定义 | 关键验证项 |
|:---:|------|-----------|
| **边界1 单元测试** | `pytest tests/`（内存/SQLite） | 默认回归与可选 CrewAI 集成需分开报告；当前 CrewAI 专项实测 **74 passed, 1 skipped**（含本地代表性端点），不等于真实权重验证。 |
| **边界2 Mock/沙箱** | preview mock LLM、`sandbox_gateway`、沙箱并发 | N=256 沙箱并发 submit=1/FAIL 收敛 compensated；RpoExceeded 告警真实运行态闭环（合成钻取源）；能力矩阵/写门控 |
| **边界3 PostgreSQL/RLS 实测** | 真实 PG 数据面 | RLS 动态 B1–B5 全拦；真实 PG 并发 N=256；DR 加密恢复 RTO=0.643s/RPO≤900s 上界；全新库 clean migrate exit 0。★取证多为 preview+一次性独立测试库（非 `after-sales-prod` 专栈实机，生产栈数据面复验＝部署期执行项） |
| **边界4 真实生产外部依赖** | 真实资产/生产实机 | **全部 BLOCKED-需外部**：受信 CA/域名、真实权重模型端点、真实资金渠道、书面确认真实租户、7 天观察 |

### 模型与评测状态

| 状态 | 允许用途 | 当前证据 |
|---|---|---|
| `mock` | 单元测试/回归；不可进入写白名单 | `MockLLM` 与 `self_hosted_server.py` 代表性端点 |
| `representative_self_hosted` | 协议、SSE、异常和安全链路演示 | 本地 OpenAI-compatible 端点；非真实权重 |
| `real_weight_candidate` | 真实权重端点评测后，候选写白名单 | 当前未完成 |
| `production_approved` | 正式生产放量 | 当前不适用 |

## 四、已验证（GO 的能力基础 ✅）

无审批绕过（唯一 `human_approval`，无 direct 边）· 无跨租户（RLS FORCE+NOBYPASSRLS，B1–B5 全拦，跨租户 404+越权留痕）· 无重复执行（并发 N=256 submit=1，跨租户幂等键不覆盖）· 可对账（沙箱级，mismatch→HUMAN_HANDOFF+审计留痕）· 具备回滚/暂停/人工接管（DR 恢复演练 RTO/RPO 实测）· 只读+shadow 先于 live（`EXECUTION_MODE=shadow`，未切 live）· 生产栈容器级健康**已实测/已证实**（**一次 bring-up 观测，T7**，见 `PROD_STACK_HEALTH.md`；api `/api/healthz`=200，nginx 8080/8843；**非当前运行态**）· 发布基线忠实（rc1→cd743d3，含迁移修复）。

## 五、未验证 / 需外部输入（阻断正式放量 ❌，含责任人 + 解锁条件）

| 项 | 成因（边界4） | 责任人 | 解锁条件 |
|---|---|---|---|
| G10 / A9 受信 TLS | 现有证书自签（CN=preview.local），无受信 CA 链 | deploy-engineer | 真实域名 + 受信 CA fullchain + `nginx -t` + `openssl s_client`(`:8843`) 复核 |
| G13 / A1 真实租户书面确认 | `LAUNCH_ALLOWED_TENANTS=__NONE_APPROVED_YET__`、`AUTH_LOGIN_CREDENTIALS={}`，无确认函 | release-manager / 业务方 | 业务方签署确认函 + PHC 注入 + 白名单/凭据 + 成员/角色授予 + 审计 |
| G14 / A11 7 天观察 | 当前未切 live，观察期未开始 | observability-engineer | 切 live 后 ≥7 天连续观测 + 每日恢复演练 + 告警无红线 |
| G5 / A10 真实权重模型评测 | preview `LLM_BACKEND=mock`、`127.0.0.1:8001` 连接拒绝；`llm_candidate_eval.json` 仅 stub；能力矩阵/写门控 ✅ PASS 但非真实模型证明 | acceptance-engineer | 真实自托管权重端点 + `write_op_pass=true` 报告 → `HIGH_CONFIDENCE_MODELS` 显式列出 |
| G6 / A5 真实资金链路 | 生产网关沙箱未部署（preview `EXECUTION_PROVIDER=mock`）；沙箱链路代码/测试级 ✅ PASS | deploy-engineer + acceptance-engineer | 部署内网 `sandbox_gateway` + `EXECUTION_PROVIDER=sandbox_http` + 端到端复跑 + shadow-only 提交 |

## 六、部署期执行项（非边界4外部因，但未本机闭环 ⚠️）

- G9/A8 生产栈**运行时** RLS / 告警端到端复验（容器级健康已证实，数据面/运行时复验待部署后实跑）。
- G2 镜像 digest **clean-context 字节级重建**（Docker engine 对当前非提升 token 拒连：`npipe:////./pipe/dockerDesktopLinuxEngine` `permission denied`；需提升完整 Admin token 或加入 `docker-users` 组 → `git archive release/v1.0.0-rc3 | tar -x` + `compose build --no-cache --provenance=false --sbom=false`）。
- G8 告警演练其余规则（依赖 `status`/`human_intervention_total` 等标签）＋ `METRICS_ALLOWED_SOURCES` 网段修正（`172.30.0.0/16`→实际内网）＋ alert-rules 权威文件对齐；G15 mismatch 计数后台实测。
- `pre_deploy_checks.sh` 本沙箱**无 docker/bash 无法实跑**（静态：仓库不干净→FAIL、compose/secret→CANNOT RUN、总体 BLOCKED）；运行证据需在**具备 docker+bash+PG 的目标服务器**补全（部署期执行项，**未宣称已通过**）。

## 七、达标判定

- ✅ **已提交收口**：阶段一 rc3 发布基线收口提交已纳入（测试默认写临时目录 + 真实 390/34 口径 + uv.lock 不入库），**基线提交时刻工作区 clean**（以 `release/v1.0.0-rc3` 为锚）。**当前 working tree 因团队证据编辑非 clean**。
- ✅ 重定稿 `GO_NO_GO.md` / `FINAL_ACCEPTANCE.md`，统一四证据边界，消除"已完成/未接入"矛盾（修正 rc1→cd743d3、A8/G9 生产栈健康已证实、迁移版本 4 点表述；**rc3 以基线尖端为锚** 口径）。
- ✅ 统一 `README.md` 生产候选版口径；历史 rc4 基线数字保留为历史记录，当前工作树全量回归为 **414 passed / 34 skipped**（开启代表性 CrewAI 集成，EXIT=0；2026-09-05）。
- ✅ `release/v1.0.0-rc3` 标签（→ 基线尖端）；镜像 digest、迁移版本（4 点）与 tag 一致。
- ✅ 所有未验证项均有明确责任人 + 解锁条件（见 §五）。

> **诚实声明**：镜像 digest 为工作树构建观测值（代码==cde30fb 运行面基线），**非 clean-context 字节级复现**；该重建列为部署期执行项。**切勿将本状态说明中的"生产候选"误读为"已生产放行"**——正式放量当前 NO-GO，须 §五 外部输入到位 + §六 复验通过后转有条件 GO。
