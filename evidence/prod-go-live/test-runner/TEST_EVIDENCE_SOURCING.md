# T1.5 · 发布基线实测「390 passed / 34 skipped」可溯源核实报告

> 任务条目：核实 `pytest tests/` 的「**390 passed, 34 skipped**（424 collected，54.63s，EXIT=0；发布基线环境实测）」这一断言是否可溯源。
> 核实方式：检索 `evidence/` 与全工作区的真实 pytest 汇总日志；用源码结构 + `--collect-only`（只收集、不跑用例）交叉验证计数；**未重新运行全量测试**，未虚构测试计数。
> 核实成员：qa-runner（测试验证）· attempt 1 · task t3
> 结论速览：**「320/70 errors → 环境问题」有真实日志强佐证**；**「424 collected」「34 skipped=33 PG+1 CrewAI」结构性可验证但无运行日志枚举**；**「390 passed / 54.63s / EXIT=0 / 发布基线环境实测」目前无任何 pytest 汇总日志佐证（属既有说明推算口径）**。

---

## 一、断言所涉数字的溯源状态总览

| 数字 / 断言语义 | 断言出处 | 有无真实日志佐证 | 佐证日志 | 溯源判定 |
|---|---|---|---|---|
| `320 passed, 34 skipped, 70 errors` | README/PROJECT_STATUS 所述「此前记录」 | ✅ 有 | `evidence/forensics_pytest_run.log`：`320 passed, 34 skipped, 70 errors in 28.85s` | **可溯源** |
| 「70 errors 均属沙箱清理 `tmp_path` 的 `PermissionError [WinError 5]` 环境问题、0 个真失败」 | README/PROJECT_STATUS | ✅ 有（强佐证） | 同上；`WinError 5` 出现 **70** 次 = 70 个 error；70 个全部是 `ERROR at setup`；traceback 均落在 `_pytest/pathlib.py ... make_numbered_dir_with_cleanup`（`tmp_path` 建临时目录），路径为受限沙箱 `...\Temp\dsh-XXXX\pytest-of-...` | **可溯源** |
| `424 collected` | README/PROJECT_STATUS | ✅ 结构性可验证（新复核） | 本次 `pytest --collect-only -q` → **`424 tests collected in 0.34s`**（只收集，不跑用例，不计入「重跑」） | **可再现** |
| `34 skipped = 33 项 PostgreSQL 数据面（无 DATABASE_URL）+ 1 项 CrewAI` | README/PROJECT_STATUS | ⚠️ 结构性可验证 | 无任何运行日志逐项枚举 skipped；由源码结构计数得出 **33 项 PG + 1 项 CrewAI** | **结构性一致（无运行日志）** |
| `test_hardening_acceptance.py:384` 为唯一 CrewAI 跳过 | 多处 | ✅ 源码确认 | `tests/test_hardening_acceptance.py:384`：`@pytest.mark.skipif(True, reason="本环境未安装 crewai；真实调用链在具备 crewai 的环境用集成测试验证")` | **可溯源（源码/静态）** |
| `390 passed` | README/PROJECT_STATUS | ❌ **无** | 全工作区（`evidence/**/*.log` + 全树 grep）**未发现**任何 pytest 汇总日志记录 `390 passed` | **不可溯源（纯叙述）** |
| `54.63s` | README/PROJECT_STATUS | ❌ **无** | 无任何日志记录 54.63s（真实日志时长为 28.85s / 21.64s / 17.45s / 8.14s） | **不可溯源（纯叙述）** |
| `EXIT=0` | README/PROJECT_STATUS | ⚠️ 无对应日志 | 无「390/34」运行日志可核验其 EXIT；仅有 clean 的 197 项日志（196/1）符合 EXIT=0 | **不可溯源（无对应运行日志）** |
| `390 passed / 1 skipped`（非 PG 口径） | `deploy/records/PRODUCTION_ACCEPTANCE_VERIFICATION.md:9` | ❌ **无** | 同上 | **不可溯源（纯叙述）** |

---

## 二、「320 passed / 70 errors」→ 环境问题：强日志佐证

`evidence/forensics_pytest_run.log`（251,974 B）末尾：

```
320 passed, 34 skipped, 70 errors in 28.85s
```

抽取证据（PowerShell 计数）：

- `WinError 5` 出现次数 = **70** —— 与「70 errors」**逐一对应**。
- `ERROR at setup` 出现次数 = **70** —— 全部为 `setup` 阶段错误，`ERROR at teardown` = 0。
- 错误异常文本（逐条）：
  ```
  E  PermissionError: [WinError 5] 拒绝访问: 'C:\Users\...\AppData\Local\Temp\dsh-Xxxxx\pytest-of-...'
  ```
  （日志内为 `拒绝访问` 的 mojibake，实际为 `Access is denied`，WinError 5。）
- 全部 traceback 收敛于：
  ```
  .venv\Lib\site-packages\_pytest\pathlib.py:420: in make_numbered_dir_with_cleanup
      raise e
  ```
  即 `tmp_path` 基目录 `pytest-of-<user>` 在受限沙箱 `dsh-*` 临时目录下创建/清理被操作系统拒绝。

**结论**：`320 passed / 34 skipped / 70 errors` 及其「70 errors 全部是沙箱清理 `tmp_path` 的 `PermissionError [WinError 5]` 环境问题、0 个真失败」的归因，**有真实日志 + 逐条计数强佐证，可溯源**。该日志给出的时长 `28.85s` 与本次断言中的 `54.63s` 不同（见 §五），恰说明该日志是**早先的沙箱受限运行**，并非「发布基线环境」的运行。

---

## 三、「34 skipped = 33 项 PostgreSQL 数据面 + 1 项 CrewAI」核实

### 3.1 CrewAI 跳过（1 项，静态确认）
`tests/test_hardening_acceptance.py:384`：

```python
@pytest.mark.skipif(True, reason="本环境未安装 crewai；真实调用链在具备 crewai 的环境用集成测试验证")
def test_crewai_real_call_chain_integration():
```

`skipif(True, ...)` → 无条件跳过，与本环境是否装 crewai 无关（`True` 硬编码）。**确认：唯一无条件跳过，即 1 项 CrewAI。**

### 3.2 PostgreSQL 数据面跳过（33 项，结构性计数）
跳过机制有两类，均以「无 `DATABASE_URL`」为触发条件：
1. `tests/conftest.py:81` 的 `pg_engine` fixture 在 `make_pg_engine()` 返回 `None` 时 `pytest.skip(...)`；`pg_app_engine` 依赖 `pg_engine`、`pg_store` 依赖 `pg_app_engine`，故凡使用这三个 fixture 的用例全部跳过。
2. `@pytest.mark.postgres` + `@pytest.mark.skipif(not pg_available())` / `module` 级 skipif（如 `test_pg_callback_concurrency.py:39-40`、`test_rls_bypass.py:31-32`、`test_recovery_consistency.py:121-122`）。

对 `tests/` 全量源码 AST 统计「PG 依赖 / postgres 标记 / pg-skipif」测试函数 = **33 项**：

| 测试文件 | 计数 |
|---|---|
| `test_pg_callback_concurrency.py` | 7 |
| `test_pg_rls.py` | 4 |
| `test_pg_store.py` | 5 |
| `test_pg_checkpoint_recovery.py` | 3 |
| `test_pg_data_source_acceptance.py` | 3 |
| `test_pg_hardening.py` | 3 |
| `test_rls_bypass.py` | 5 |
| `test_data_source_contract.py` | 2 |
| `test_recovery_consistency.py` | 1 |
| **合计** | **33** |

**33（PG）+ 1（CrewAI）= 34 跳过**，与断言一致。

**注意（诚实标注）**：该「33」由源码结构**推导**得出，且是基于「无 DATABASE_URL」这一配置的**推断**（假设 33 个 PG 用例都会在无 DB 时被 skipif/fixture 跳过）。**没有任何实际运行日志逐项列出这 34 个 skipped 的 node id**，因此「34 skipped」本身**无运行日志枚举佐证**；只能确认其与「424 collected / 33+1」在结构上自洽。

---

## 四、「424 collected」核实

本次在 `tests/` 执行（**只收集、不跑用例**）：
```
$ python -m pytest --collect-only -q
424 tests collected in 0.34s
```
说明「424 collected」有**可再现的收集计数**支撑（排除 parametrize 展开影响后，源码 `def test_`/`async def test_` 约 419 个函数 + 少量 parametrize，收集为 424，可见 parametrize 展开约 5 项）。该计数与断言一致，**非虚构**。

---

## 五、「390 passed / 54.63s / EXIT=0 / 发布基线环境实测」溯源：**无日志佐证**

### 5.1 检索范围
- `evidence/**/*.log` 全量 glob：`pg_migrate.log`、`pg_suite.log`、`pg_acceptance_full.log`、`pg_acceptance_non_tmp.log`、`pg_acceptance_full_v2.log`、`prod-go-live/deploy-engineer/MIGRATE_FAILURE.log`、`_verify_llm_chain_run.log`、`forensics_pytest_run.log`。
- 全工作区 grep：`390 passed`、`424 collected`、`54.63`、`EXIT=0`、`passed, 34 skipped`。

### 5.2 发现
- **没有任何 pytest 汇总日志记录「390 passed」**。唯一带 `passed` 汇总的日志为：
  - `evidence/forensics_pytest_run.log`：`320 passed, 34 skipped, 70 errors in 28.85s`（沙箱受限运行）。
  - `evidence/pg_acceptance_full_v2.log`：`196 passed, 1 skipped in 21.64s`（**clean 重跑，但收集仅 197 项**，见 §六）。
  - `evidence/pg_acceptance_non_tmp.log`：`176 passed, 1 skipped in 17.45s`。
  - `evidence/pg_suite.log`：`12 passed`（PG 标记子集）。
- **没有任何日志记录「54.63s」**（真实时长 28.85 / 21.64 / 17.45 / 8.14s 均不同于 54.63s）。
- **没有任何「390/34」运行日志可核验其 `EXIT=0`**。
- `390 passed` / `54.63s` / `EXIT=0` / 「发布基线环境实测」 仅以叙述形式出现在 `README.md:105`、`PROJECT_STATUS.md:26/56`、`deploy/records/PRODUCTION_ACCEPTANCE_VERIFICATION.md:9` 等**文档文件**中。

### 5.3 判定
“**390 passed, 34 skipped（424 collected，54.63s，EXIT=0；发布基线环境实测）**”这一完整断言，**目前不可溯源到任何真实 pytest 汇总日志**。它是「既有说明口径」的**推算/表述**，而非「新日志佐证的实测」。

**可信部件**：`424 collected`（本次收集复核通过）；`34 skipped = 33 PG + 1 CrewAI`（结构自洽）。
**无日志部件**：`390 passed`、`54.63s`、`EXIT=0`、以及「发布基线环境实测」这一性质的声明。

---

## 六、内部口径不一致（需修正，避免误用）

1. **`pg_acceptance_full_v2.log` 被多处标注为「全量 pytest 套件」**（`PG_ACCEPTANCE_REPORT.md:81`、`PG_CHECKPOINT_RECOVERY_RECORD.md:45`、`PG_RLS_POLICY_INVENTORY.md:20` 等），但其汇总为 `196 passed, 1 skipped`（共 **197** 项收集），**远小于** collect-only 的 **424**。即：该日志**不是**「全量 424」覆盖，而是约 197 项的**子集**运行（或口径不同）。文档中「全量」一词与 `424 collected` 相互矛盾。
2. **两种「390」口径并存**：`README.md` / `PROJECT_STATUS.md` 采用「390 passed **/ 34 skipped**（全量 424）」；`deploy/records/PRODUCTION_ACCEPTANCE_VERIFICATION.md:9` 采用「**非 PG 测试 390 passed / 1 skipped（需 PG）**」。两者被当作同一基线引用时会造成 34 与 1 的混乱。
3. **时长对不上**：断言 `54.63s` 与任何真实日志时长（28.85 / 21.64 / 17.45 / 8.14s）都不相符。

建议：统一「全量 / 基线实测」用词；为每个 pytest 日志标注**实际收集范围**与**触发命令**；将「390/34」与其源日志明确绑定，或标记为「推算口径，待发布基线环境重跑归档」。

---

## 七、让它可溯源的方式（诚实建议，本次未执行）

要产出一份**可溯源**的「发布基线环境实测 390/34」证据，需在**发布基线环境**（`venv` 直跑、非受限沙箱、**无 `DATABASE_URL`**）执行一次全量：

```
.venv\Scripts\python.exe -m pytest tests/ -q             # 期望汇总 424 collected, 390 passed, 34 skipped
```

并将 stdout 重定向归档（如 `evidence/prod-go-live/test-runner/pytest_baseline_full.log`），确保含有 `424 collected`、`390 passed, 34 skipped` 与时长、`EXIT=0`。**本核实未执行上述运行**，也未虚构任何计数；理由遵循任务约束「不要为了凑数而重新跑全量测试，除非已有日志」。

---

## 八、核实者使用的证据与工具

- `evidence/forensics_pytest_run.log`（320/34/70 权威运行日志）
- `evidence/pg_acceptance_full_v2.log`（196/1）、`evidence/pg_acceptance_non_tmp.log`（176/1）、`evidence/pg_suite.log`（12 passed）
- `tests/test_hardening_acceptance.py:384`（CrewAI skipif）
- `tests/conftest.py:59-106`（PG 标记 / `pg_engine` skip 逻辑）
- `tests/test_pg_*.py`、`test_rls_bypass.py`、`test_data_source_contract.py`、`test_recovery_consistency.py`（33 项 PG 用例来源）
- `pytest --collect-only -q` → 424 collected（本次复核，只收集）
- 交叉核对：`README.md:105`、`PROJECT_STATUS.md:20/26/54/56`、`deploy/records/PRODUCTION_ACCEPTANCE_VERIFICATION.md:9`、`evidence/PG_ACCEPTANCE_REPORT.md:79-89`、`evidence/PG_CHECKPOINT_RECOVERY_RECORD.md:45`、`evidence/PG_RLS_POLICY_INVENTORY.md:20`
