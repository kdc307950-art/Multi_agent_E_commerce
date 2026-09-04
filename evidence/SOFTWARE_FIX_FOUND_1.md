# FOUND-SOFTWARE-1 修复记录

> 发现者：security-auditor（t5 动态验收）　修复者：security-auditor（t9）　状态：✅ 已修复 + 回归验证 + 重建生效
> 关联任务：t5（发现）→ t9（修复）。仓库文件：`src/infrastructure/postgres_store.py`。

---

## 1. 缺陷描述

**「PostgresStore 对 psycopg3 已自动解析为 dict 的 JSONB 列二次 json.loads」**

`src/infrastructure/postgres_store.py` 在读取 PostgreSQL 的 JSONB 列时重复调用 `json.loads`。psycopg3 驱动会按列类型（JSONB/JSON）**自动把值解析为 Python dict/list**，而代码又 `json.loads(...)` 一次，抛出：

```
TypeError: the JSON object must be str, bytes or bytearray, not dict
```

### 受影响位置（共 5 处，均为 `json.loads` 接收已解析 dict）

| 位置 | 列 | 函数 |
|---|---|---|
| L298（修复前） | `operations.result` | `_row_to_operation` |
| L549 | `executions.receipt` | `_row_to_execution` |
| L551 | `executions.compensation_result` | `_row_to_execution` |
| L612 | `stream_events.data` | `events_after` |
| L636 | `audit.detail` | `list_audit` |

> 注：`src/infrastructure/sqlite_store.py` 中同名 `json.loads` **不改**——SQLite 经 `sqlite3` 返回 TEXT 字符串，`json.loads` 是正确的；差异源于驱动行为（sqlite=text 字符串，psycopg3=自动解析 dict）。

### 造成的实际后果（真实 Postgres 数据面，t5 实测）

1. **审批通过后 `execute_refund` 收尾崩溃**：执行成功 → `update_operation(..., EXECUTED)` → `_row_to_operation` 读 `result` 时 `json.loads` 崩溃 → 异常被 `nodes.py` 的失败兜底捕获 → `operation status=FAILED` + 转人工。**把本应成功的资金执行误判为失败。**
2. **SSE 重放崩溃**：`run_resume` / 同 `client_request_id` 去重重放 → `events_after` 读 `data` 崩溃。
3. **`/api/audit` 查询崩溃**：`list_audit` 读 `detail` 崩溃。

属**资金/执行路径的阻断性缺陷**。

---

## 2. 修复

### 2.1 根因

psycopg3 对 JSONB 列返回已解析 dict；代码 `json.loads(dict)` 必然抛 TypeError。SQLAlchemy 作中间层不改变这一驱动行为。

### 2.2 修复方案

新增模块级安全解析助手 `_as_json`，**仅在值为 str/bytes 时才 `json.loads`，已是 dict/list（或 None）则原样返回**：

```python
def _as_json(value):
    if value is None:
        return None
    if isinstance(value, (str, bytes, bytearray)):
        return json.loads(value)
    return value
```

### 2.3 Diff 要点（5 处读取统一改为 `_as_json`）

```
 src/infrastructure/postgres_store.py
@@ def _row_to_operation:
-  result=json.loads(row["result"]) if row["result"] else None
+  result=_as_json(row["result"])
@@ def _row_to_execution:
-  receipt=json.loads(row["receipt"]) if row["receipt"] else None,
-  compensation_result=json.loads(row["compensation_result"]) if row["compensation_result"] else None,
+  receipt=_as_json(row["receipt"]),
+  compensation_result=_as_json(row["compensation_result"]),
@@ def events_after:
-  "data": json.loads(r["data"]), ...
+  "data": _as_json(r["data"]), ...
@@ def list_audit:
-  detail=json.loads(r["detail"]), ...
+  detail=_as_json(r["detail"]), ...
```

### 2.4 一致性核对

- `engine.py` / `approval` 读取这些 JSONB 列均经 `store.get_operation` / `store.get_execution_record` / `store.get_approval` 等 store 方法收敛，已统一切换到 `_as_json`，无需另行改动。
- `engine.py` L289 / `api/routes.py` L392 / `auth/security.py` L185-186,270 的 `json.loads` 均作用于 HTTP body / base64 / 文件字符串，与 JSONB 列无关，**不改**。

---

## 3. 回归验证

### 3.1 内存数据面 pytest（宿主 venv）

```
tests/test_approval_idempotency.py  +  tests/test_execution_engine.py
37 passed in 1.38s
```
覆盖：退款必经审批 / 重复决策幂等 / 拒绝不执行 / 跨租户 operation_id 唯一 / 并发 CAS 单抢占 / 拒绝标记 / 超时转人工 / 绑定校验 / 低档模型拒绝 / shadow 幂等 / live 回调重放 / 坏签名 / 金额不匹配转人工 / 补偿 / 对账 / 跨租户回调 404 / provider 仅调一次 / 超时未确认转人工。**全部通过，无回归。**

### 3.2 容器内真实 Postgres 端到端（修复后 api 容器）

经 `TestClient`（真实 PostgresStore + real JWT）走「会话→退款申请→审批通过→execute_refund 收尾」：

| 检查点 | 结果 |
|---|---|
| `op_before` | `pending`（审批前未执行） |
| `approve_status` | 200，返回同一 `operation_id` |
| **`op_after`** | **`executed`**（关键：不再误标 FAILED） |
| `replay_status` | 200（SSE 重放 `events_after` 不再崩溃） |
| `audit_status` | 200（`/api/audit` 不再崩溃，可定位 1 条） |

### 3.3 数据面落库核实（修复后）

```
operations: fe71a388... status=executed, result.execution_status=confirmed,
            message="退款/退货/改址已在 shadow 模式下生成模拟回执。"
executions: 635d0a8b... mode=shadow, status=confirmed,
            idempotency_key=oprefund:TENANT-A:ORD-001:T9-E2E-1d2212b2
重复执行检查: GROUP BY operation_id HAVING count(*)>1  -> 0 行
```
确认：审批通过后执行**成功收敛为 confirmed/executed**，不再因 JSONB 崩溃误判失败；无重复执行。

---

## 4. 让修复在运行栈生效

1. 重建镜像：`docker compose --env-file deploy/.env.preview -f docker-compose.preview.yml build api worker` → 成功（含仓库修复源码）。
2. 强制重建容器：`docker compose ... up -d --force-recreate api worker` → api/worker 均 `healthy`。
3. **确认容器内已是仓库修复版**（容器内 `postgres_store.py`）：`grep _as_json` 命中 L45 定义 + L313/564/566/627/651 五处读取，且**无**旧运行时补丁的内联 `isinstance(...)` 形式。重建覆盖了 t5 期间打的运行时临时补丁，容器内现为纯净仓库版。
4. 整栈确认：`api/worker/frontend/nginx/postgres/redis` 全部 `healthy`；nginx 仍仅暴露 80/443。

## 5. 协调记录（与 t6 DR 演练）

- 重建前确认了 api/worker 已稳定运行（4 分钟 healthy，无正在进行的演练操作）。
- 已向 drill-runner 发送协调消息说明 t9 将重建 api/worker，请其避让。重建动作为 `build + --force-recreate api worker`，耗时约 30 秒，api/worker 短暂重启后恢复 healthy。
- 若 t6 演练已运行，因其目标是"备份恢复/api/worker/redis 重启/回滚"，与本次重建存在窗口重叠风险；已通过消息协调，且重建后栈已恢复全 healthy，未对演练造成持久影响。（如有需要，请 drill-runner 复核其演练记录与本次重建时序。）

---

## 5.5 核验澄清（针对「是否误报」的疑问，git 决定性证据）

**结论：FOUND-SOFTWARE-1 是真实缺陷，t9 修复是必要且有效的，非误报。** 队长核验到的「容器内已全是 `_as_json`」正是本次 t9 修复+重建的结果，而非「当前代码本来正确」。

关键事实（以 git 实测为准，`git -C <repo>` 运行）：

- **HEAD 提交 `251f430` 的仓库源码 = 缺陷版**。`git show HEAD:src/infrastructure/postgres_store.py` 显示 5 处 JSONB 读取仍为**无防御**写法：
  ```
  result=json.loads(row["result"]) if row["result"] else None
  receipt=json.loads(row["receipt"]) if row["receipt"] else None,
  compensation_result=json.loads(row["compensation_result"]) if row["compensation_result"] else None,
  "data": json.loads(r["data"]), ...
  detail=json.loads(r["detail"]), ...
  ```
- **当前工作区 = 修复版**。`git diff src/infrastructure/postgres_store.py`（本文件 §2.3）清晰呈现 `+`（新增 `_as_json` 助手）+ 5 处 `-`改`+`。`git status` 显示该文件为 `M`（相对 HEAD 已修改）。
- **运行容器 = 工作区修复版**：重建 `build api worker` 时 COPY 的是当前工作区（已含 `_as_json`），故容器 grep `_as_json` 命中。

**为什么队长核验会误判为「当前代码本来正确」**：队长用 `git log` 看提交（`git log` 只有 1 个 commit 且为 `251f430`，因为我的修复**尚未 commit**，只是工作区 `M` 状态），于是误以为「容器 HEAD=251f430 与仓库一致 → 当前代码正确」。但 `git log` 的提交对象仍是**修复前**的源码；`git show HEAD:<file>` 才是提交里的真实内容（缺陷版）。容器内之所以有 `_as_json`，是因为它跑的是**工作区（我改后）**而非 HEAD 提交。

> 一句话：不是「当前代码本来就有 `_as_json`」，而是「我 t9 修好后重建，容器才带上 `_as_json`」。若不做这次修复、仅用 HEAD(251f430) 源码重建，容器会复现 `json.loads(dict)` TypeError。

---

## 6. 结论

FOUND-SOFTWARE-1 已**在仓库源码中修复**（新增 `_as_json` 防御式解析，5 处 JSONB 读取切换），**内存 pytest 37/37 通过**，**容器内真实 Postgres 端到端验证通过**（审批→execute 收尾 `executed`、SSE 重放、/api/audit 正常），并**重建 api/worker 镜像 + 重启使修复在运行栈生效**（容器内确认仓库版代码）。资金/执行路径不再因 JSONB 重复解析而误标失败。

### 结论定性（队长复核后确认）

**FOUND-SOFTWARE-1 = HEAD(251f430) 真实缺陷 → 修复（工作区未提交 `_as_json`，+20/-5）→ 重建生效 → E2E 通过。**

- 队长复核确认：`git show HEAD:.../postgres_store.py` **无 `_as_json`**（真实缺陷）；本次修复为工作区 **`+20/-5` 未提交**修复（新增 `_as_json` 助手 + 5 处读取切换）+ `build/rebuild api worker` 重建生效；E2E `approval→executed` 通过、无重复执行。队长已更正此前"疑似误报"的判断并致谢。
- 当前状态：**无需再改源码**，修复与验证到位。

### 提交后状态（已 commit）

- **commit hash：`7941246`** —— `fix(execution): 防御 JSONB 列在 psycopg3 下重复 json.loads — 新增 _as_json,仅 str/bytes 才解析,修复审批后 execute_refund 收尾/SSE 重放//api/audit 崩溃`（仅 `src/infrastructure/postgres_store.py`，1 file changed, +20/-5）。
- **提交后 HEAD `7941246` 即含修复**：`git show HEAD:src/infrastructure/postgres_store.py` 已含 `def _as_json(value)` 及 `_as_json(row["result"])`/`_as_json(row["receipt"])`/`_as_json(row["compensation_result"])`/`_as_json(r["data"])`/`_as_json(r["detail"])`。放量审计版本可控（`src/infrastructure/postgres_store.py` 工作区 clean；`migrations.py` 为先前会话改动，**未一并提交**，符合"不夹带无关改动"要求）。
- **HEAD 含修复验证计数**：`git show HEAD:src/infrastructure/postgres_store.py | grep -c _as_json` = **6**（= `def _as_json(value)` 定义 1 处 + 5 处 `_as_json(...)` 读取；≥1，确认 HEAD 已含修复）。
- 运行容器（api/worker）即该修复版代码（重建时 COPY 工作区），重建生效。若后续以此为基线放量，审计以 `7941246` 为版本锚点。

*产出：本文件 + 修复后的 `src/infrastructure/postgres_store.py`（已 commit `7941246`）。HEAD 现含修复，放量审计版本可控。*
