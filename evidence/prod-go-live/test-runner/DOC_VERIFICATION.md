# T1.4 · README / PROJECT_STATUS 与 rc3 口径一致性核对报告

> 任务条目：核对 `README.md` 与 `PROJECT_STATUS.md` 是否与 rc3 口径一致（5 项）。
> 核对方式：全文读取两文档；用 git 实况（tag 解析、commit 链、working tree）与证据（`PROD_STACK_HEALTH.md`、`IMAGE_DIGESTS.json`、`PG_ACCEPTANCE_REPORT.md` 等）交叉核对；并**交叉引用 T1.5 测试溯源结论**（`evidence/prod-go-live/test-runner/TEST_EVIDENCE_SOURCING.md`）。
> **追更（队长实测后，本报告据此定案）**：队长在发布基线环境实跑全量 pytest，得权威结果 **`390 passed, 34 skipped in 53.83s`，EXIT=0，424 collected，34 skipped=33 PG(缺 DATABASE_URL)+1 CrewAI**，日志 `evidence/prod-go-live/test-runner/pytest_full_rerun.log`（本报告已逐条核验：34 条 SKIPPED 中 33 条为 PG/DATABASE_URL、1 条为 crewai `test_hardening_acceptance.py:384`，汇总行=「390 passed, 34 skipped in 53.83s」）。据此：**本报告原「★ §3.1 390/34 需队长裁决」已获实质解答**（390/34 确有可溯源日志），并在队长指导下完成对 README/PROJECT_STATUS 的修正（见下方「已实施修正」）。
>
> **已实施修正（本轮，qa-runner 执行）**：
> 1. 测试口径：两文档 `54.63s` → **`53.83s`**，并补「**发布基线环境实测，见 `evidence/prod-go-live/test-runner/pytest_full_rerun.log`**」；保留「34 skipped=33 PG 无 DATABASE_URL+1 CrewAI」说明及「此前 `320 passed,34 skipped,70 errors` 属沙箱 tmp_path PermissionError 环境问题」的历史说明。涉及：README L105、PROJECT_STATUS L26、PROJECT_STATUS L56（补 424 collected/53.83s/EXIT=0/日志引用）。
> 2. 「全容器健康/运行中」补**观测时点**限定（按 t5 §2.3 建议、并采用队长最终措辞）：改为「**容器级健康已实测/已证实**（**一次 bring-up 观测，T7**，见 `PROD_STACK_HEALTH.md`；**非当前运行态**）」。涉及：README L4/L110/L117、PROJECT_STATUS L33。
> 3. git 状态（PROJECT_STATUS L20/L54）：加「**基线时刻 clean**（rc3 锚 `3ccab5c` 工作区当时 clean）；**当前 working tree 因团队发布证据编辑非 clean**」限定。
> 4. NO-GO + 外部依赖 BLOCKED-需外部：核验准确，保留不变（两文档一致）。
>
> 仅改 README.md / PROJECT_STATUS.md；未改 src / tag / 分支。
>
> > 核对者：qa-runner（测试验证）· attempt 1 · task t8
> 说明：本核对**未修改** `README.md` / `PROJECT_STATUS.md`（见 §四）。

---

## 一、五项核对结果

| # | 核对项 | README.md | PROJECT_STATUS.md | 判定 |
|---|--------|-----------|-------------------|------|
| ① | 测试数 = "390 passed, 34 skipped (424 collected, 54.63s, EXIT=0, 发布基线环境实测)"，无 320/70 残留 | 行105：**有 390/34 口径**；但**保留**"此前记录的 320 passed, 34 skipped, 70 errors"作历史说明 | 行26：**有 390/34 口径**；但**保留**"此前记录的 320 passed, 34 skipped, 70 errors"作历史说明 | ⚠️ **需队长裁决**（数字口径一致，但见 §三-1 的溯源问题） |
| ② | 34 skipped 理由（33 PG 无 DATABASE_URL + 1 CrewAI）完整准确 | 行105：33 项 PG（无 DATABASE_URL）+ 1 项 CrewAI（`test_hardening_acceptance.py:384`，本环境未安装 crewai） | 行26：33 项 PostgreSQL 数据面（无 DATABASE_URL）+ 1 项 CrewAI 真实调用链（`test_hardening_acceptance.py:384`） | ✅ 一致（两文档一致且与 T1.5 结构验证相符）；仅 crewai 原因是代码 `skipif(True)` 无条件跳过、非"本环境未装"，但因 `reason` 字符串即"本环境未安装 crewai"，措辞与代码一致（轻微精度，见 §3.3） |
| ③ | 无"当前全容器健康"等无法由运行态证明的表述 | 行4/110/117：**有**"实测全容器健康 / 全容器健康（边界3 专栈实机）"，引用 `PROD_STACK_HEALTH.md`（T7） | 行33：**有**"生产独立栈**全容器健康**（`PROD_STACK_HEALTH.md`，api /api/healthz=200，nginx 8080/8843）" | ⚠️ **需修正/需队长裁决**（证据支撑但为时点观测，见 §二-1） |
| ④ | 放量 NO-GO 且外部依赖明确 BLOCKED-需外部 | 行4/117：NO-GO；边界4 五项外部依赖（A9/G10 TLS、A10/G5 权重模型、A1/G13 租户、A11/G14 观察、A5/G6 资金）全部为待外部输入 | 行10/29/35-43：NO-GO；§四 边界4 全部 **BLOCKED-需外部**（受信 CA/域名、权重模型、资金渠道、书面租户、7 天观察）；§五 每项有责任人+解锁条件 | ✅ 完全一致 |
| ⑤ | 发布标签/锚 = release/v1.0.0-rc3 | 行4/112：`release/v1.0.0-rc3`（→ 基线尖端，见 git tag）；rc1→cd743d3、rc2→290b830→fa7c9a3 | 行16-17：`release/v1.0.0-rc3`＝annotated tag 对象 `d1d2867`→commit `3ccab5c`(==HEAD)；rc1→cd743d3；rc2→290b830→fa7c9a3 | ✅ 已用 git 实测复核（见 §二-2） |

---

## 二、关键项明细

### 2.1 ③「全容器健康」表述（⚠️ 需注意）
- 两文档均以**现在/达成时态**呈现"全容器健康"，并引用 `evidence/prod-go-live/deploy-engineer/PROD_STACK_HEALTH.md`（T7，deploy-engineer 记录）。
- 该证据**确实记录了** `docker compose ps` 全部 `Up (healthy)` + nginx/api `/healthz` 200，故**并非无依据断言**。
- **但**属于 **T7 一次性时点实测**（一次性观测），并非"当前持续运行态"；且本沙箱**无 docker 可用**（`docker: not recognized`），无法从运行态复核"当前全容器健康"。
- **已有限定**：`PROJECT_STATUS.md §六` G9/A8 明确"容器级健康已证实，**数据面/运行时复验待部署后实跑**"——即健康是"已证实"，但生产栈的运行时 RLS/告警端到端复验是部署期执行项。README 行117 也写明"生产栈运行时 RLS/告警复验 + clean-context 字节级重建后转有条件 GO"。
- **判定**：表述有证据支撑且已被 PROJECT_STATUS 限定到"容器级已证实"，未虚报。**建议**：措辞强调"**已实测/已证实**"（T7），避免让读者误认为"当前正在运行/可随时复核"；本项目当前无持续运行时复验证据。

### 2.2 ⑤ rc3 锚：git 实测复核（✅）
| 项 | 文档陈述 | git 实测 | 结果 |
|---|---|---|---|
| rc3 tag→commit | tag 对象 `d1d2867` → commit `3ccab5c`（==HEAD） | `release/v1.0.0-rc3^{commit}` = `3ccab5c`；HEAD=`3ccab5c` | ✅ 一致 |
| rc1 | → `cd743d3` | `release/v1.0.0-rc1^{commit}` = `cd743d3`（BUSINESS_DATA_BACKEND 提交，subject 吻合） | ✅ 一致 |
| rc2 | tag 对象 `290b830` → commit `fa7c9a3` | `290b830`=tag；`release/v1.0.0-rc2^{commit}` = `fa7c9a3` | ✅ 一致 |
| 提交链 | `7941246`/`3268a1c`/`696444a`/`8ddca48`/`cd743d3`/`cde30fb`/`59e37f2`/`bca4861`/`fa7c9a3` 均存在 | 逐一 `git cat-file -t` 均为 `commit` | ✅ 一致 |

**结论**：发布锚（`release/v1.0.0-rc3` → 基线尖端 = HEAD `3ccab5c`）在 git 中**可实测复现**，两文档陈述正确。

### 2.3 ⚠️ 「git 状态 clean」声明（PROJECT_STATUS 行20/54）与当前 working tree 不符
- `PROJECT_STATUS.md` 行20/54 声明 **git 状态 clean**（HEAD=基线尖端）。
- 但当前 `git status --short` 显示 **M**（已修改）：`PROJECT_STATUS.md`、`README.md`、`evidence/.../IMAGE_DIGESTS.json`、`evidence/.../FINAL_ACCEPTANCE.md`、`evidence/.../GO_NO_GO.md`；以及 **??**（未跟踪）：`_pytest_tmp/`、`test-runner/TEST_EVIDENCE_SOURCING.md`（本任务 t3 产出）等多个新文件。
- **性质判断**：这些改动多为**当前团队多智能体正在协同编辑/产出的工作树状态**（release-manager/deploy-engineer/security-auditor 均在运行中），**非 rc3 基线提交本身的问题**（rc3 提交 `3ccab5c` 时的 baseline 是 clean 的）。
- **⚠️ 提示**：作为"当前状态"表述，`git clean` 此刻不成立；建议在该声明处明确"基线时刻 clean"或归因于进行中的团队产出，避免误读。

---

## 三、需队长裁决项（★ 重要）

### 3.1 ★「390/34 发布基线环境实测」溯源问题（criterion ① 的实质）
- 两文档均以"**全量实测 390 passed, 34 skipped**（424 collected，54.63s，EXIT=0；**发布基线环境实测**）"作主口径，并将 `320 passed, 34 skipped, 70 errors` 归为"**此前记录**…沙箱清理 `tmp_path` 的 `PermissionError [WinError 5]` 环境…故修正后实测 390/34"。
- **T1.5 关键发现**：`390 passed / 34 skipped / 54.63s / EXIT=0` 在整个工作区**无任何 pytest 汇总日志佐证**；真正有日志的是 `evidence/forensics_pytest_run.log`＝`320 passed, 34 skipped, 70 errors in 28.85s`（WinError5 恰 70 次=70 error，全为 tmp_path setup 环境问题）与 `evidence/pg_acceptance_full_v2.log`＝`196 passed, 1 skipped in 21.64s`（收集仅 197 项，非 424）。
- **结论**：两文档所述"**发布基线环境实测 390/34**"有**真实日志支撑的是"320/70"**而非"390/34"；"390/34"目前是**既有说明的推算口径**，其"实测"表述**无可溯源日志**。这正好与 criterion ①"无 320/70 残留"形成张力：**320/70 才是被日志证明的实测值**，390/34 是叙述推算值。
- **请队长/团队裁决**（二选一）：
  - (A) 在发布基线环境（venv 直跑、非受限沙箱、无 `DATABASE_URL`）**真正跑一次全量并归档日志**，让"390/34/54.63s/EXIT=0"可溯源；
  - (B) 若暂不重跑，将两文档该数字从"**发布基线环境实测**"降级为"**推算/既有口径（待发布基线环境重跑归档）**"，并保留 320/70 作为"此前实测值"的诚实说明。
- 该改动**影响整个基线的"达标"陈述**，超出"小改"范围，故**本次未改动**，交队长裁决。

### 3.2 「320/70 残留」措辞取舍（合并到 3.1）
- 若严格按"无 320/70 残留"，两文档当前都**保留了**"320 passed, 34 skipped, 70 errors"（作历史说明）。这既是**诚实交代**（320/70 是真实日志值），也可能被读成"残留"。如何权衡由队长/团队定；默认建议保留历史说明但明确其为"此前沙箱实测值、已被后续口径取代/待重跑归档"。

### 3.3 轻微精度（② 的边界）
- `README.md` 行105 说 CrewAI 跳过是因"本环境未安装 crewai 故跳过"；代码 `test_hardening_acceptance.py:384` 为 `@pytest.mark.skipif(True, reason="本环境未安装 crewai；真实调用链在具备 crewai 的环境用集成测试验证")` —— 是 `skipif(True)` **无条件跳过**，与是否装 crewai 无关；`crewai==0.152.0` 实际在**独立 venv**（`.accept-crewai-venv`）已导入+构造通过，仅未与 langgraph 同环境共存（README 行107 也如此说明）。
- **建议**（小改，可交给 release-manager 统一）：将 CrewAI 跳过理由从"本环境未安装 crewai"细化（如"crewai 未与 langgraph 同环境验证共存，真实子智能体调用链为环境边界"）。**本次为保口径一致未改**，仅在清单列出。

---

## 四、本次未直接改动的说明

- `README.md` 与 `PROJECT_STATUS.md` **当前处于 release-manager 等成员的进行中编辑**（working tree 为 M 状态）；由 qa-runner 直接改动有冲突风险。
- 本核对发现的**不一致主要属"实质/判定级"**：① 的"390/34 实测"溯源、③ 的"全容器健康"措辞、以及"git clean"声明，均需团队/队长定夺，**非中立无争议的小改**。
- 故本报告只做核对与清单，**未改文档文字**；小改项（如 §3.3 crewai 措辞）留待队长分派。

---

## 五、交叉引用 T1.5 测试溯源

- `evidence/prod-go-live/test-runner/TEST_EVIDENCE_SOURCING.md`（task t3 产出）：核实「390 passed/34 skipped(424 collected,54.63s,EXIT=0)」。
- 关键联动结论（供队长整合）：
  1. `424 collected` 可复现（`--collect-only`=424）。
  2. `34 skipped = 33 PG + 1 CrewAI` 结构一致（AST 计数 33 PG + `skipif(True)` crewai）。
  3. `390 passed / 54.63s / EXIT=0` **无 pytest 汇总日志佐证**（纯叙述）。
  4. `320/70 errors` **有强日志佐证**（`forensics_pytest_run.log`，WinError5=70=error 数）。
  5. `pg_acceptance_full_v2.log` 被多处标"全量"但收集仅 **197** 项（196/1），与 424 矛盾——建议在引用该日志处标注真实收集范围。

---

## 六、证据 / 工具清单

- `README.md`（行 4/105/110/117）、`PROJECT_STATUS.md`（行 10/16-20/26/29/33/35-43/54-56）
- git 实况：`git rev-parse HEAD`=`3ccab5c`；`release/v1.0.0-rc3^{commit}`=`3ccab5c`；`release/v1.0.0-rc1^{commit}`=`cd743d3`；`release/v1.0.0-rc2^{commit}`=`fa7c9a3`；`290b830`=tag 对象；`git status --short`=多文件 M/??。
- `evidence/prod-go-live/deploy-engineer/PROD_STACK_HEALTH.md`（全容器健康，T7）
- `evidence/prod-go-live/deploy-engineer/IMAGE_DIGESTS.json`（rc3 运行时基线 `cde30fb`、镜像 digest 为 worktree 构建、clean-context 重建=部署期执行项）
- `evidence/prod-go-live/test-runner/TEST_EVIDENCE_SOURCING.md`（T1.5，交叉引用）
- `tests/test_hardening_acceptance.py:384`（crewai `skipif(True)`）
- `evidence/forensics_pytest_run.log`（320/34/70，WinError5）
- `evidence/pg_acceptance_full_v2.log`（196/1，收集 197）

> 注：本核对未重新运行测试，未虚构计数；`docker` 在本沙箱不可用，无法从运行态复核"当前全容器健康"。
