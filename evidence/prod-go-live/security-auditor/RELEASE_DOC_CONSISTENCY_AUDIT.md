# 发布文档口径一致性审计（T1.1）

> **归属**：security-auditor · `rc3-prod-readiness`
> **范围**：`evidence/prod-go-live/` 下全部 17 份 Markdown 发布/部署证据文档，逐一核对三类「口径失真」。
> **方法**：以 **git 运行态实测为唯一事实来源**（HEAD / tag 对象 / peeled commit / 基线链 / 镜像 digest 字段），逐文件核对 rc 版本锚点、测试数、容器健康与其他运行态表述。
> **红线**：本审计**只定位与建议修法，不改任何源文件**；仅新增本审计文档。全程未改 tag / 分支 / commit。

---

## 0. 事实基准（git 实测，唯一事实来源）

| 项 | 值（实测） |
|---|---|
| 当前 HEAD | `3ccab5ca8e59bce967c0ce986fd97bda5d7ff6df`（`3ccab5c`）；分支 `main` / `origin/main` / `codex/prod-readiness` 均在 `3ccab5c` |
| 当前发布锚点 | **`release/v1.0.0-rc3`** = annotated tag 对象 `d1d2867` → commit **`3ccab5c`** == HEAD |
| rc2 tag | annotated tag 对象 **`290b830`** → commit **`fa7c9a3`**（`release/v1.0.0-rc2^{commit}`=fa7c9a3） |
| rc2 tag 对象体信息 | `chore(release): v1.0.0-rc2 基线尖端（阶段一发布基线收口…）` |
| `bca4861` 对象类型 | **commit**（`git cat-file -t bca4861` → `commit`；`git log -1` 标题=「阶段一发布基线收口——统一发布口径 + 归档证据 + 清理临时产物」），是 `fa7c9a3` 的**父提交**，**并非 rc2 tag** |
| `bca4861` 自身提交说明 | `基准口径：HEAD=59e37f2；release/v1.0.0-rc2(annotated d2c9db1)->59e37f2`（记载的是**更早**基线态） |
| rc1 tag | annotated tag 对象 `2120c4b` → commit `cd743d3` |
| 运行时面（镜像代码）基线 | **`cde30fb`**（rc2/rc3 为 docs/tests/gitignore-only 收口，未改 src/deploy 运行面；IMAGE_DIGESTS.json `release_baseline.commit=cde30fb`） |
| 迁移修复 commit | `696444a`（`shipping_events` 复合外键修复） |
| 基线 commit 链（至 HEAD） | `7941246`→`ad168ef`/`8be2d97`/`03819a8`/`3268a1c`→`696444a`→`8ddca48`→`cd743d3`→`cde30fb`→`59e37f2`→`bca4861`→`fa7c9a3`→`602243f`→`0a3fff6`→**`3ccab5c`** |

**审计总判**：`RELEASE_CHECKLIST.md` 与 `BASELINE_LOCK.md` 为 **rc3 口径正确**的两份（当前锚点=rc3/3ccab5c）。其余多份发布/部署证据（`FINAL_ACCEPTANCE`、`GO_NO_GO`、`DEPLOY_BASELINE`、`BASELINE_COMMIT_SCOPE`、`IMAGE_DIGESTS.json`、`DR_encrypted_restore`、`OBSERVABILITY_REPORT`、`CONCURRENCY_RECOVERY_REPORT`）存在三类口径失真。**测试数（320/70）在 evidence 内已不再被当作当前值**，无残留（见 §四）。

---

## 一、类型①：仍把 rc1/rc2 当「当前 HEAD / 当前发布锚点」（应为 rc3=3ccab5c）

### 1.1 `evidence/prod-go-live/release-manager/FINAL_ACCEPTANCE.md`
| 行 | 问题 | 建议修法 |
|---|---|---|
| 1 | 标题「… 定稿 · rc2」 | 改「定稿 · rc3」；或注明「正式验收结论，rc2 期定稿，随 rc3 发布」 |
| 4 | 「随 `release/v1.0.0-rc2`（→ 基线尖端）提交」 | 改为「随 `release/v1.0.0-rc3`（=3ccab5c）提交」 |
| 5 | 「当前 HEAD = `release/v1.0.0-rc2` = 基线尖端」 | **错误**：当前 HEAD=rc3=3ccab5c。改为「当前发布锚点 = `release/v1.0.0-rc3`(3ccab5c)；本文为 rc2 期定稿，若作为 rc3 发布证据须重定稿或标注为历史锚点」 |

> 注：FINAL_ACCEPTANCE 的整体验收结论（能力/合规闭环 + 外部阻断 NO-GO）仍成立，仅**版本锚点表述**过时。

### 1.2 `evidence/prod-go-live/release-manager/GO_NO_GO.md`
| 行 | 问题 | 建议修法 |
|---|---|---|
| 12 | 「当前 HEAD = **基线尖端**（…以 `release/v1.0.0-rc2` 为锚）」 | 改为「当前 HEAD = `release/v1.0.0-rc3`=3ccab5c」 |
| 13 | 「本次 rc2 校订项 `README.md`…/`IMAGE_DIGESTS.json`（rc2 口径）」 | rc2 → rc3 口径 |
| 14 | 「后续 "rc2 commit + `git archive` clean-context 重建"」 | rc2 → rc3 |
| 67 | 小节标题「rc1 精确=cd743d3」 | 成立为**历史事实**；补一句「rc1/rc2 为历史锚点，当前锚点=rc3」 |
| 68 | 「再叠加**阶段一发布基线收口提交**（基线尖端，当前 HEAD）」 | 「当前 HEAD」删去或改「(当时 HEAD)」；当前 HEAD=3ccab5c |
| 69 | 「**本次 rc2**：`release/v1.0.0-rc2` 以**基线尖端**为锚…」 | 改为「本次 rc3」 |
| 71 | 「**正式发布 tag 为 `release/v1.0.0-rc2`**（基线尖端，以 tag 为锚）」 | 改为「正式发布 tag = `release/v1.0.0-rc3`(=3ccab5c)」 |
| 75 | 「证据（**本 rc2 观测值**，工作树构建、代码==基线）」 | rc2 → rc3 |
| 78 | 「跑通 `git archive release/v1.0.0-rc2 \| tar -x`…」 | rc2 → rc3 |
| 170 | 「`git archive release/v1.0.0-rc2` clean-context 字节级重建」 | rc2 → rc3 |
| 178 | 「## 四、tag / 镜像 digest / 迁移版本一致性（**本次 rc2 口径**）」 | rc2 → rc3 |
| 182 | 「rc2 基线 commit 链含之」 | rc2 → rc3 |
| 187 | 「`tag.commit（release/v1.0.0-rc2）== build_ref == …`」（本次 rc2 建 annotated tag） | 改 rc3，且按类型③重述不变量（tag.commit≠runtime baseline≠migrations） |

### 1.3 `evidence/prod-go-live/deploy-engineer/DEPLOY_BASELINE.md`
| 行 | 问题 | 建议修法 |
|---|---|---|
| 16 | 「发布基线 commit/tag … `release/v1.0.0-rc1`→`cd743d3`」 | 补「rc1 为历史锚点；当前运行面基线=cde30fb（IMAGE_DIGESTS release_baseline.commit）」 |
| 23 | Go/No-Go…「发布基线已忠实（`release/v1.0.0-rc1`→`cd743d3`…）」 | rc1 口径 → 标注历史；结论段补「当前锚点=rc3」 |
| 48 | 「在 **HEAD=3268a1c** 上创建…`release/v1.0.0-rc1`」 | 历史动作；明确「当时 HEAD=3268a1c（早期快照）」 |
| 49 | 「`release/v1.0.0-rc1` 已重定向到 `696444a`」 | 历史 rc1；标注「rc1 已被后续 rc2/rc3 取代，当前锚点=rc3」 |
| 59 | 「以 `git archive`（clean-context）在 `release/v1.0.0-rc1` 上重建」 | rc1 → rc3（或标注为历史说明） |

### 1.4 `evidence/prod-go-live/deploy-engineer/BASELINE_COMMIT_SCOPE.md`
| 行 | 问题 | 建议修法 |
|---|---|---|
| 4 | 「发布基线：`release/v1.0.0-rc1` → `cd743d3`（tree `9669ce25`）」 | 附注「rc1 为早期锚点；当前基线=rc3/3ccab5c，运行面基线=cde30fb + 迁移 696444a」 |
| 39 | 「需在 `release/v1.0.0-rc1` 上用 `git archive` 物化 clean-context…」 | rc1 → rc3 |

---

## 二、类型②：残留过时测试数 / 不可证实的「当前全容器健康」

### 2.1 测试数（320 passed / 70 errors）—— **无残留（已修正）**
- `RELEASE_CHECKLIST.md` 行 79 为「**390 passed / 34 skipped**（424 collected，54.63s，EXIT=0；发布基线环境实测）」；
- 行 81 为「修正说明：先前 `320 passed, 34 skipped, 70 errors` 均为受限沙箱清理 `tmp_path` 的 `PermissionError [WinError 5]` 环境问题（70 errors 均环境问题、0 真失败），故修正为 **390/34**」。
- **结论**：`320/70` 仅作为「被修正掉的旧值」**以说明性文字**出现，**未作为当前测试数被断言**；与 rc3 tag 消息「真实 390/34 口径」一致。**此项通过，无需整改。**

### 2.2 过时的 HEAD 快照（当前 HEAD 已推进，旧快照不可再当「当前」）
| 文件:行 | 问题 | 建议修法 |
|---|---|---|
| `DEPLOY_BASELINE.md`:31 | 「分支 `main`，**HEAD = 3268a1c**…」 | 当前 HEAD=3ccab5c。改为「当时(2026-09-04 T1) HEAD=3268a1c」或同步为 rc3 |
| `DR_encrypted_restore.md`:5 | 「应用 git 引用：**3268a1c**」 | 注明「本次演练时点 HEAD=3268a1c（历史快照）；当前 HEAD=3ccab5c」 |
| `DEPLOY_BASELINE.md`:45,53 | 「HEAD（3268a1c）不是已接受系统的忠实快照」「以最新可复现构建点 HEAD」 | 均为**当时** HEAD 论证；加「当时」限定以免被误读为当前 |

### 2.3 不可证实的「当前全容器健康 / 运行中」表述（是"观测时点"证据，非"当前运行态"证明）
以下以**现在时/完成时**表述容器健康或运行态，但**本质是一次 bring-up / 探测观测快照**；自当前静态证据无法重证「此刻仍全部 healthy / 仍在运行」。建议统一**限定为观测时点**并注明「未经本次复核」。

| 文件:行 | 现表述 | 建议修法 |
|---|---|---|
| `PROD_STACK_HEALTH.md`:14,44 | 行44「独立生产栈（project=after-sales-prod）**全部容器健康**」；行14「全栈容器状态（`docker compose ps`，全部健康）」 | 改为「本次(2026-09-04 T7) bring-up 观测：全容器 healthy；**当前运行态健康未经本次复核**」（附 compose 输出时点） |
| `DEPLOY_BASELINE.md`:18,21,23,174,175,176 | 行18「**全栈健康**：compose 迁移 exit 0 + …全 healthy」；行21「**生产栈健康 + 门控确认**…全容器 healthy」 | 加「观测时点（T7 bring-up）」限定；注明非当前运行态证明 |
| `GO_NO_GO.md`:53,83,88,112 | 行112「完整 after-sales-prod 栈带起健康：✅ 已证实（PROD_STACK_HEALTH.md，T7）」；行53「+ 完整 prod 栈带起健康已证实」 | 补「(T7 bring-up 观测)」；与 OBSERVABILITY_REPORT「生产栈未部署」割裂（见 §三.3） |
| `FINAL_ACCEPTANCE.md`:36,76,100,109 | 行76「完整 after-sales-prod 栈带起健康：✅ 已证实（…T7）」 | 同上补「(T7 bring-up 观测)」 |
| `OBSERVABILITY_REPORT.md`:13,14,15,24,123 | 行13「after-sales-preview **运行中（7 容器健康）**」；行14「…运行中（10 个容器…健康）」；行15「after-sales-prod（生产栈）**未部署**」 | 「运行中/健康」标注为「观测时点」；行15与 PROD_STACK_HEALTH 相矛盾（见 §三.3） |
| `CONCURRENCY_RECOVERY_REPORT.md`:13,14,15,193 | 行13「生产栈 after-sales-prod **未部署**」；行14「运行中的栈…」 | 同上；标注「观测时点」，并与 T7 生产栈健康证据 reconcile |
| `MIGRATE_VERIFY.md`:10 | 迁移镜像 digest `5d39f030…` | 注明「一次性独立测试库 `after-sales-prodtest-pg`（边界3），非 after-sales-prod 专栈实机」（与 GO_NO_GO 边界口径一致，建议口径统一） |

---

## 三、类型③：tag / commit / runtime_source 关系描述不一致

### 3.1 `release-manager/BASELINE_LOCK.md` §4（行 118–167）—— 关键
- 行 120「任务既定口径为 "rc2 历史锚定 `bca4861`/`fa7c9a3`"。**实测与既定口径不完全一致**」；行 127「`bca4861` 对象类型 = `commit`（不是 tag）」；行 151「`bca4861` 不是 rc2 的 tag 对象；它是一个 commit」；行 152「当前 rc2 真实 tag 对象 = `290b830` → commit `fa7c9a3`」；行 154「把 `bca4861` 记为 "rc2 tag" 是文档口径偏差」。
- **判定**：BASELINE_LOCK 的**实测事实正确**（与本次独立复核一致）。**问题在于**：它把「任务既定口径 bca4861/fa7c9a3」与「实测 290b830→fa7c9a3」并列，而**未给出定稿结论**（行 154：「请队长/测试或人工确认」）。这使 rc2 锚点**悬而未决**，成为后续文档漂移的根源。
- **建议修法**：在此定稿口径——**「rc2 不可移动历史锚点 = release/v1.0.0-rc2 tag 对象 `290b830` → commit `fa7c9a3`」**；`bca4861` 仅记为「阶段一收口**父提交**」（其自身记载更早基线 d2c9db1→59e37f2，属历史演进记录，非当前口径）。删除「待人工确认」字样，避免证据悬置。

### 3.2 `deploy-engineer/IMAGE_DIGESTS.json` 行 8（note）—— 与 3.1 冲突
- 行 8 note：「exactly as established at rc2 (**tag=release/v1.0.0-rc2 -> bca4861**; runtime baseline = cde30fb)」。
- **问题**：把 rc2 tag 记为 `bca4861`，**与 BASELINE_LOCK/RELEASE_CHECKLIST 的实测（290b830→fa7c9a3）冲突**；`bca4861` 实为 commit。
- **建议修法**：改「(tag=release/v1.0.0-rc2 -> **fa7c9a3**（tag 对象 290b830）; runtime baseline = cde30fb)」。

### 3.3 `release-manager/GO_NO_GO.md` 行 187 / `RELEASE_CHECKLIST.md` 行 103 / `IMAGE_DIGESTS.json` 行 5,8 —— 不变量与运行面基线割裂
- `GO_NO_GO.md` 行 187（及 `RELEASE_CHECKLIST.md` 行 103）断言统一不变量：**`tag.commit（release/v1.0.0-rc2/rc3）== build_ref == migrations_schema_source == config_version_source`**。
- 但实测：`tag.commit(rc3)=3ccab5c`；`build_ref`/运行面基线=`cde30fb`（IMAGE_DIGESTS `release_baseline.commit=cde30fb`，行 5）；`migrations_schema_source=696444a`（migrations.py）；`config_version=0.1.0`。
- **问题**：四者**并不相等**（rc3 是 docs/tests/gitignore-only 收口，未改 src/deploy 运行面；`release_baseline.commit` 记录的是运行面代码基线 `cde30fb`，且 IMAGE_DIGESTS 行 8 明言「A commit cannot reference its own hash, so release_baseline.commit (image code baseline) and the release tag are intentionally distinct」）。因此**把「tag.commit == build_ref」写成统一不变量是错误/可能导致校验误判**。
- **建议修法**：将「统一不变量」改写为**分离变量**，如：
  - `release-tag.commit(rc3) = 3ccab5c`（发布证据/收口提交）
  - `runtime-surface baseline = cde30fb`(src/deploy 运行面；rc2/rc3 未变) == `IMAGE_DIGESTS.release_baseline.commit`
  - `migrations.schema source = 696444a`(migrations.py)
  - `config_version = 0.1.0`
  - 结论：「**tag 所在 commit 与运行面基线允许不同**（rc3 为 docs-only 收口）；校验时须**分别**核 `release/v1.0.0-rc3^{commit}==3ccab5c`、`IMAGE_DIGESTS.release_baseline.commit==cde30fb`、迁移 696444a、config 0.1.0，**不可再假设四者相等**。」

### 3.4 `release-manager/RELEASE_CHECKLIST.md` 行 20（caveat）与行 35
- 行 20「rc3 not re-built (digest observation value **inherited from rc1/rc2 code baseline**)」；行 35「运行面（镜像代码）基线 = **`cde30fb`**… 代码基线沿用 rc2 即 `cde30fb`（IMAGE_DIGESTS.json release_baseline.commit）」。
- **问题**：digest 为「工作树构建、代码==基线」，且明确「未按 rc3 重新构建、值为 rc1/rc2 基线沿用」——即 **digest 与 rc3 tag 的映射是「沿用/继承」，并非「rc3 现场构建」**。这会让读者误以为该 digest 是 rc3 的产物。
- **建议修法**：在 §2 表头加「**digest 观测值沿用 rc1/rc2 运行面基线（cde30fb）构建，非 rc3 现场构建**」；与 IMAGE_DIGESTS caveat 口径一致。

---

## 四、跨文档一致性矩阵（矛盾点汇总）

| 主题 | 文档A（说A） | 文档B（说B） | 是否矛盾 | 处理建议 |
|---|---|---|---|---|
| **当前发布锚点** | FINAL_ACCEPTANCE :5 / GO_NO_GO :12,71 -> 当前=rc2/基线尖端 | RELEASE_CHECKLIST :4,34 / BASELINE_LOCK :19,24 -> 当前=rc3=3ccab5c | ⚠️ 矛盾（A 过时） | 以 rc3 为准，修正 A |
| **生产栈是否部署/健康** | OBSERVABILITY_REPORT :5,15,24,123「生产栈**未部署**（无运行容器）」；CONCURRENCY_RECOVERY_REPORT :13「生产栈未部署」 | PROD_STACK_HEALTH :14,44「全部容器健康」；DEPLOY_BASELINE :18,174-176「全栈健康已达成」；GO_NO_GO :112「栈带起健康已证实」 | 🔴 **直接矛盾** | 需 reconcile：观测报告/并发报告为**早期(t3/t4)时点**（production 尚未 bring-up），PROD_STACK_HEALTH 为**后续 T7 bring-up**；建议统一改为「生产栈于 T7 完成一次 bring-up 观测（全部 healthy）；t3/t4 时点未部署」并在各文件标注各自时点 |
| **rc2 tag 对象** | IMAGE_DIGESTS :8「rc2 tag->bca4861」 | BASELINE_LOCK :126,152「rc2 tag 对象=290b830->commit fa7c9a3」；RELEASE_CHECKLIST :94「实际 repo tag 对象 290b830 -> fa7c9a3」 | 🔴 矛盾（IMAGE_DIGESTS 错） | 以 290b830->fa7c9a3 为准，修正 IMAGE_DIGESTS :8 |
| **统一不变量** | GO_NO_GO :187 / RELEASE_CHECKLIST :103「tag.commit==build_ref==migrations==config」 | IMAGE_DIGESTS :5,8「release_baseline.commit=cde30fb (≠ tag commit 3ccab5c)，有意区分」 | ⚠️ 不一致 | 按 §3.3 分离变量，废除「四者相等」表述 |
| **镜像 digest 归属** | RELEASE_CHECKLIST :20「rc3 not re-built，digest 沿用 rc1/rc2 基线」 | GO_NO_GO :183「digest 均自基线 commit 构建」 | ⚠️ 口径需明确 | 统一为「digest 观测值=运行面基线(cde30fb)工作树构建，非 rc3 现场重建」 |

---

## 五、rc2 作为「历史锚定(不可移动)」的确认结论（任务子问题）

**结论：保留「rc2 不可移动历史锚点」的定位是正确的、恰当的**；但**具体哈希标注需修正**，不应原样保留「`bca4861`/`fa7c9a3`」这对标签：

1. ✅ **「不可移动」正确**：`release/v1.0.0-rc1`/`rc2`/`rc3` 及 `baseline-prod-*` 均为仅走 `git tag` 建立的**不可变锚点**，后续发布不再移动它们；`RELEASE_CHECKLIST`（§0.2 行 24）「发布 tag 不可移动/删除」的红线是正确且必要的，应保留。
2. ⚠️ **「rc2 = bca4861/fa7c9a3」标签不精确**：
   - **权威 rc2 commit 锚点 = `fa7c9a3`**（`release/v1.0.0-rc2^{commit}`，tag 对象 `290b830`）。这一点已被 BASELINE_LOCK 与 RELEASE_CHECKLIST 确认，且与 rc2 tag 对象体吻合。
   - **`bca4861` 是 commit（阶段一收口），是 `fa7c9a3` 的父提交，不是 rc2 tag**；其自身提交说明还记载了**更早**的基线「rc2 = annotated d2c9db1 → 59e37f2」——属于**历史演进记录**，不是当前口径。
   - 因此「rc2 历史锚定(bca4861/fa7c9a3)」应改为：**「rc2 历史锚点 = `release/v1.0.0-rc2`(tag 对象 `290b830` → commit `fa7c9a3`)，不可移动；`bca4861` 为其父提交(阶段一收口)，仅作历史说明」**。
3. **行动建议**：在 `BASELINE_LOCK.md` §4 将「待人工确认」settle 为上述权威口径；同步修正 `IMAGE_DIGESTS.json` 行 8 的「rc2 tag->bca4861」为「rc2 tag->fa7c9a3(tag 对象 290b830)」。

（本审计**未移动/删除任何 tag**，仅记录口径结论。）

---

## 六、优先级与整改责任归属

| 优先级 | 整改项 | 归属 |
|---|---|---|
| 🔴 高 | §3.3 统一不变量「tag.commit==build_ref==migrations==config」改为分离变量（否则放量前校验会误判） | release-manager（定稿）+ deploy-engineer（IMAGE_DIGESTS） |
| 🔴 高 | §四 生产栈「未部署 vs 全容器健康」跨文档矛盾 reconcile（各标注观测时点） | release-manager + deploy-engineer + observability/test-runner |
| 🔴 高 | §3.1/§3.2 rc2 锚点定稿（fa7c9a3/tag 对象 290b830；修正 IMAGE_DIGESTS :8） | release-manager（BASELINE_LOCK §4 定稿） |
| 🟡 中 | §一 全部 rc1/rc2 当「当前」的版本锚点表述改为 rc3 | release-manager（FINAL_ACCEPTANCE/GO_NO_GO）+ deploy-engineer（DEPLOY_BASELINE/BASELINE_COMMIT_SCOPE） |
| 🟡 中 | §2.2 陈旧 HEAD 快照（3268a1c）加「当时/时点」限定 | deploy-engineer + test-runner（DR_encrypted_restore） |
| 🟡 中 | §2.3「全容器健康/运行中」统一加「观测时点、未经当前复核」限定 | deploy-engineer + test-runner + observability |
| 🟢 低 | §2.1 测试数无残留（390/34 正确）—— 通过，无需整改 | — |

---

## 七、审计局限性
- 本审计基于**本地 Git + 文件只读核验**与各证据文档**静态内容比对**；**未实跑生产编排**、未联机确认容器当前健康、未对 IMAGE_DIGESTS/环境做重放。
- 「当前运行态健康」为**不可由静态证据重证**的表述，本审计只就其**表述口径**提出「限定观测时点」建议，**未断言**容器此刻是否真的健康。
- 未修改任何源文档、tag、分支、commit；新增的唯一文件即本审计产出。

---

*证据：`git rev-parse HEAD`、`git for-each-ref`（heads/tags）、`git cat-file -p release/v1.0.0-rc{1,2,3}`、`git log --oneline --graph`、`git cat-file -t bca4861`、`IMAGE_DIGESTS.json` 与各证据文档逐行比对。仅定位，不改文件。*
