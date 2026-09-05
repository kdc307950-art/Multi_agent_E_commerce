# TEST_EVIDENCE_RC4 — RC4-candidate 权威测试证据（阶段一收口）

> **⚠️ 已被取代**：本文件记录**阶段一 rc4-candidate（396/34）**的历史权威口径；**阶段二~四综合态（rc5-candidate）权威口径为 `411 passed / 34 skipped / EXIT=0`**，见 `TEST_EVIDENCE_RC5.md`（单一权威数字以 RC5/`RC4_PROD_READINESS_MASTER_REPORT.md` 为准）。
> **角色**：release-manager（发布经理）· rc4-productionization 团队 · 任务 t1
> **用途**：固化 `pytest tests/` 的**唯一权威测试快照**，作为 `release/v1.0.0-rc4-candidate` 基线的"单元测试链路"收口证据。
> **诚实声明**：本文件只陈述**已有运行日志**真正支持的计数（来自 captain 实测日志 `pytest_captain_baseline.log`），**不虚构**任何"已达标/已放量"结论；真实模型、真实租户、受信 CA、目标服务器、7 天 shadow 仍为 **BLOCKED/NO-GO**。

---

## 0. 结论速览（唯一权威口径）

| 项 | 值 |
|---|---|
| 权威运行日志 | `evidence/prod-go-live/test-runner/pytest_captain_baseline.log` |
| 收集数（collected） | **430**（= 396 passed + 34 skipped；clean 运行，无 errors/failures，故 collected == passed + skipped） |
| 通过（passed） | **396** |
| 失败（failed） | **0** |
| 跳过（skipped） | **34** |
| 耗时 | 41.92s |
| 退出码（EXIT） | **0** |
| 执行 commit（基线绑定） | `release/v1.0.0-rc4-candidate`（`git rev-parse release/v1.0.0-rc4-candidate^{commit}`；即阶段一 rc4-candidate 冻结提交，其工作树 == captain 实测时的树） |
| 命令大意 | `.venv` 直跑、非受限沙箱、无 `DATABASE_URL` 的发布基线环境；`pytest tests/ -q` |

**单一口径声明**：**396 passed / 0 failed / 34 skipped / EXIT=0 / 41.92s** 为阶段一 RC4-candidate 基线**唯一权威测试口径**。凡文档与 `390 passed / 53.83s`（`pytest_full_rerun.log`，不同运行、被本次权威运行取代）不一致处，以本文为准；**禁止在同一发布证据集内混用多套数字**。

---

## 1. 权威运行小结（pytest_captain_baseline.log 末尾）

```
........................................................................ [ 16%]
.......................ss............................................... [ 33%]
..................................s..................................... [ 50%]
...................................................................sssss [ 66%]
ssssssssssssssssssss.........ssssss..................................... [ 83%]
......................................................................   [100%]
396 passed, 34 skipped in 41.92s
```

> `.`=passed，`s`=skipped；无 `F`（失败）与 `ERROR`，故 failed=0、errors=0，`EXIT=0`（captain 实测确认）。

---

## 2. 34 skipped 构成（可追溯）

34 skipped = **33 项 PostgreSQL 数据面** + **1 项 CrewAI 真实调用链**。

### 2.1 CrewAI 跳过（1 项 · 静态确认）
- `tests/test_hardening_acceptance.py:384`：`@pytest.mark.skipif(True, reason="本环境未安装 crewai；真实调用链在具备 crewai 的环境用集成测试验证")`
- **原因**：本环境未安装 `crewai`（`skipif(True)` 无条件跳过），真实 CrewAI 子智能体调用链留待具备 crewai 的集成环境验证。

### 2.2 PostgreSQL 数据面跳过（33 项 · 无 `DATABASE_URL`）
> 触发条件：`tests/conftest.py` 的 `pg_engine` fixture 在 `make_pg_engine()` 返回 `None` 时 `skip(...)`；`@pytest.mark.postgres` + `skipif(not pg_available())`。全部原因 = **PostgreSQL 未配置，缺少 `DATABASE_URL` 未连接数据库（docker compose up -d postgres）**。以下逐项由 `pytest_full_rerun.log` 的 short test summary info 枚举（与权威运行同为 33 PG + 1 CrewAI 构成）。

| 测试文件 | 跳过的 node 行号 | 计数 |
|---|---|---|
| `tests/test_data_source_contract.py` | 293、319 | 2 |
| `tests/test_pg_callback_concurrency.py` | 88、135、172、232、269、294、323 | 7 |
| `tests/test_pg_checkpoint_recovery.py` | 61、81、95 | 3 |
| `tests/test_pg_data_source_acceptance.py` | 62、79、95 | 3 |
| `tests/test_pg_hardening.py` | 31、58、83 | 3 |
| `tests/test_pg_rls.py` | 24、33、44、56 | 4 |
| `tests/test_pg_store.py` | 17、35、46、61、69 | 5 |
| `tests/test_recovery_consistency.py` | 120 | 1 |
| `tests/test_rls_bypass.py` | 61、74、89、109、136 | 5 |
| **PG 合计** | | **33** |

| 类别 | 计数 |
|---|---|
| PostgreSQL 数据面跳过 | 33 |
| CrewAI 跳过 | 1 |
| **skipped 合计** | **34** |

---

## 3. 证据绑定与一致性

- **证据绑定同一 commit**：本文件所引用的 `396 passed / 34 skipped / EXIT=0` 计数，绑定到 `release/v1.0.0-rc4-candidate`（阶段一冻结提交）。该提交的工作树（src/deploy/测试）与 captain 实测时的树一致；随冻结提交纳入本证据文件，使"基线 commit ↔ 测试计数 ↔ 证据"三点绑定。
- **单一权威数字**：全仓库发布证据统一为 396/0/34/EXIT=0/41.92s，未混用 390/424/53.83s 等旧口径。
- **skipped 原因可追溯**：33 PG（无 `DATABASE_URL`）+ 1 CrewAI（无 crewai），均列出 node 位置与原因。
- **范围如实标注**：PG 跳过因缺 `DATABASE_URL`，属**发布基线环境**（无 PG）预期；放量前须在具备 PG 的生产栈环境启用 `DATABASE_URL` 后重跑**数据面子集**。这**不改变** 396/34 的单元测试收口口径。

---

## 4. 完整 stdout 归档

- 路径：`evidence/prod-go-live/test-runner/pytest_captain_baseline.log`
- 内容：pytest 全量运行 stdout（condensed progress + 汇总行：`396 passed, 34 skipped in 41.92s`）。
- 退出码：`0`（captain 实测）。
- 执行 commit：`release/v1.0.0-rc4-candidate`（见 §0）。

---

## 5. 诚实边界（不撒谎）

- **未**重新运行全量测试；本文件基于**已有权威运行日志** `pytest_captain_baseline.log` 组织，未虚构计数。
- 收集数 430 由权威运行日志汇总（396 passed + 34 skipped、无 errors/failures）推导，非日志直接打印的 `collected` 字段。
- 「已达标/已放量」**未达**：真实模型、真实租户、受信 CA、目标服务器、7 天 shadow 仍为 **BLOCKED / NO-GO**（见 `GO_NO_GO.md`）。本文件仅固化**单元测试链路**的权威计数。
