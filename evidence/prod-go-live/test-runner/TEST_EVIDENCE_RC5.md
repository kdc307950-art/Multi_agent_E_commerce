# TEST_EVIDENCE_RC5 — RC5-candidate 权威测试证据（阶段二~四综合收口 · 唯一权威口径）

> **角色**：release-manager（发布经理）· rc4-productionization 团队 · 任务 t6
> **用途**：固化 **阶段二~四综合态**下 `pytest tests/` 的**唯一权威测试快照**，作为 `release/v1.0.0-rc5-candidate` 基线的"单元测试链路"收口证据。
> **与 RC4 的关系**：本文件**新增**，作为**当前（阶段二~四综合）权威口径**；`TEST_EVIDENCE_RC4.md` 保留**阶段一 rc4-candidate（396/34）**的历史记录，已被本文件**取代**（阶段二~四新增 15 个通过用例）。
> **诚实声明**：只陈述**已有运行日志**真正支持的计数（captain/本 release-manager 实测），**不虚构**任何"已达标/已放量"；真实模型、真实租户、受信 CA、目标服务器、7 天 shadow 仍为 **BLOCKED/NO-GO**。

---

## 0. 结论速览（唯一权威口径）

| 项 | 值 |
|---|---|
| 权威运行日志 | `evidence/prod-go-live/test-runner/pytest_rc5_final.log`（`pytest tests/ -q`） |
| 收集数（collected） | **445**（= 411 passed + 34 skipped；clean 运行，无 failures/errors，故 collected == passed + skipped） |
| 通过（passed） | **411** |
| 失败（failed） | **0** |
| 跳过（skipped） | **34** |
| 耗时 | 37.19s |
| 退出码（EXIT） | **0** |
| 执行 commit（基线绑定） | `release/v1.0.0-rc5-candidate`（`git rev-parse release/v1.0.0-rc5-candidate^{commit}`；即阶段二~四综合冻结提交，其 src/deploy/tests 树 == 实测时的树） |
| 命令大意 | `.venv` 直跑、非受限沙箱、无 `DATABASE_URL` 的发布基线环境；`pytest tests/ -q` |

**单一口径声明**：**411 passed / 0 failed / 34 skipped / EXIT=0** 为**阶段二~四综合（rc5-candidate）唯一权威测试口径**。阶段一 rc4-candidate 为 **396/34**（历史、已被取代）；t4/t5 期间 security-auditor/crewai-engineer 曾观测 **406/34**，且 security-auditor 记录为 **406 passed, 34 skipped**、crewai-engineer 记录为 **406 passed, 1 skipped, 33 deselected**（非 PG 口径）——这些是**工作过程中的中间观测**，均被本次**最终权威运行 411/34** 取代。**禁止在同一发布证据集内混用多套数字。**

---

## 1. 权威运行小结（pytest_rc5_final.log 末尾）

```
........................................................................ [ 16%]
......................................ss................................ [ 32%]
.................................................s...................... [ 48%]
........................................................................ [ 64%]
..........sssssssssssssssssssssssss.........ssssss...................... [ 80%]
........................................................................ [ 97%]
.............                                                            [100%]
411 passed, 34 skipped in 37.19s
```

> `.`=passed，`s`=skipped；无 `F`（失败）与 `ERROR`，故 failed=0、errors=0，`EXIT=0`。
> 补充：为逐项获取 skipped 原因，另以 `pytest tests/ -q -rs` 复跑确认，结果为 **411 passed, 34 skipped in 42.11s，EXIT=0**（计数一致，仅时长因环境抖动不同）。

---

## 2. 34 skipped 构成（可追溯 · 逐项枚举自 -rs 运行）

34 skipped = **33 项 PostgreSQL 数据面** + **1 项 CrewAI 真实调用链**。

### 2.1 CrewAI 跳过（1 项）
- `tests/test_hardening_acceptance.py:394`：`需要 crewai 和自托管 LLM 端点；安装 crewai 并置 RUN_CREWAI_INTEGRATION=1 运行`
- **原因**：本环境未安装 `crewai`、无真实自托管 LLM 端点 → 真实 CrewAI 子智能体端到端链路留待具备 crewai + 真实端点的集成环境验证。

### 2.2 PostgreSQL 数据面跳过（33 项 · 无 `DATABASE_URL`）
> 触发条件：`tests/conftest.py` 的 `pg_engine` fixture 在 `make_pg_engine()` 返回 `None` 时 `skip(...)`；`@pytest.mark.postgres` + `skipif(not pg_available())`。全部原因 = **PostgreSQL 未配置，缺少 `DATABASE_URL` 未连接数据库（docker compose up -d postgres）**。

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
| CrewAI 跳过（无 crewai / 无自托管端点） | 1 |
| **skipped 合计** | **34** |

---

## 3. 与阶段一（rc4-candidate）的差异

| 项 | rc4-candidate（阶段一） | rc5-candidate（阶段二~四综合） | 说明 |
|---|---|---|---|
| passed | 396 | **411** | **+15**：新增 `tests/test_crewai_main_path.py`（10 例）及阶段二~四其它新增/修复通过的用例 |
| skipped | 34 | 34 | 构成不变：33 PG 无 `DATABASE_URL` + 1 CrewAI 无 crewai |
| failed | 0 | 0 | 无回归 |
| EXIT | 0 | 0 | 一致 |
| autoritative log | `pytest_captain_baseline.log` | `pytest_rc5_final.log` | |

**结论**：阶段二~四**未破坏**阶段一 396/34 基线，仅在综合态**净增 15 个通过用例**（含 10 个 CrewAI 主链路验收用例），无新增失败/失败回归。

---

## 4. 证据绑定与一致性

- **证据绑定同一 commit**：本文件 `411/34/EXIT=0` 计数绑定 `release/v1.0.0-rc5-candidate`（阶段二~四综合冻结提交），其 src/deploy/tests 树与实测时一致。
- **单一权威数字**：全仓库 rc5-candidate 发布证据统一为 **411/0/34/EXIT=0**；阶段一 396/34 作为历史基线**明确标注被取代**，未与之冲突。
- **skipped 原因可追溯**：33 PG（无 `DATABASE_URL`）+ 1 CrewAI（无 crewai / 无自托管端点），全部列出 node 位置与原因（§2）。
- **范围如实标注**：PG 跳过因缺 `DATABASE_URL`，属发布基线环境（无 PG）预期；放量前须在具备 PG 的生产栈环境启用 `DATABASE_URL` 后重跑 **数据面子集**（部署期执行项）。**不改变** 411/34 的单元测试收口口径。

---

## 5. 完整 stdout 归档

- 路径：`evidence/prod-go-live/test-runner/pytest_rc5_final.log`（`pytest tests/ -q`）
- 退出码：`0`（实测）。
- 执行 commit：`release/v1.0.0-rc5-candidate`（见 §0）。

---

## 6. 诚实边界（不撒谎）

- **未**虚构任何计数；基于**已有权威运行日志** `pytest_rc5_final.log` 组织。
- 收集数 445 由权威运行日志汇总（411 passed + 34 skipped、无 failures/errors）推导，非日志直接打印的 `collected` 字段。
- **「已达标/已放量」未达**：真实自托管模型端点、真实租户书面确认、受信 CA/域名、目标服务器、7 天 shadow、真实资金 live、真实渠道均仍为 **BLOCKED / NO-GO**（见 `RC4_PROD_READINESS_MASTER_REPORT.md`）。本文件仅固化**单元测试链路**的权威计数。
