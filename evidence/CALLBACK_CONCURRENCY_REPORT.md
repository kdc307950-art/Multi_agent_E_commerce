# 回调并发/验签/重放验收报告（G6 安全回退）

> 验证人：gw-verify（test-runner） ｜ 日期：2026-09-04 ｜ 涉及：回调验签、非重放窗口、终态封闭、金额冲突转人工、跨租户伪造拒绝。
> 佐证：`pytest tests/test_callback_concurrency_cas.py tests/test_callback_security_log.py` → **13 passed**。
> 引擎层探针：`evidence/callback_g6_verify.json`（SQLite 后端，`ExecutionEngine.apply_callback`）。

## 0. 结论（TL;DR）

**通过。** 回调链路在**验签、重放、冲突**三个维度全部 fail-closed：

| 场景 | 引擎结果 | 判定 | 说明 |
|---|---|---|---|
| 错误签名 | `signature_invalid`，`applied=False` | ✅ 拒绝 | HMAC-SHA256 常量时间比对失败即拒 |
| 同 nonce 重投 | `replay`，`applied=False` | ✅ 只重放不重复生效 | 非重放窗口：与已记账 nonce 一致→重放 |
| 终态后新 nonce | `terminal_locked`，`applied=False` | ✅ 终态封闭 | 终态绝不覆盖（防重复扣款/退款） |
| 金额不一致 | `amount_mismatch` → 执行 `mismatched`、操作 `human_handoff` | ✅ 转人工 | 不落入错误的账户操作 |
| 未知 status | `processing`（保持 `submitted`、仅记账 nonce） | ✅ 中间态 | 等待最终回调；不擅自跃迁 |
| 跨租户伪造 | `not_found`，`applied=False`，原记录零污染 | ✅ 拒绝码 | 不泄露存在性、不写审计 |

## 1. 验签与重放保护实现

- `src/execution/verification.py`：
  - `verify_hmac_signature`：HMAC-SHA256 + `hmac.compare_digest` 常量时间比对；缺签名/缺密钥一律 `False`（fail-closed）。
  - `normalize_callback_payload`：外部 `status` 仅 `succeeded/success/confirmed` → `CONFIRMED`、`failed/rejected/refunded` → `FAILED_DISPATCHED`，其余（`processing/pending/unknown`）归一化为**中间态**（保持 `submitted`）。
- `src/execution/engine.py` `apply_callback`：先验签 → 解析 `tenant_id/execution_id/nonce`（缺失即 `missing_identity`/`missing_nonce`）→ 委托 `store.apply_callback_atomic` 原子应用。
- `store.apply_callback_atomic`（三后端同 `plan_callback_atomic` 计划，**同一事务**完成「nonce 记账 + 状态 CAS 终态封闭 + 操作更新 + 审计写入」）。

## 2. 引擎层探针证据（`evidence/callback_g6_verify.json`）

| 项 | 实际值 | 断言 |
|---|---|---|
| 错误签名 | `reason=signature_invalid, applied=False` | ✅ |
| 同 nonce 重投 | 首次 `confirmed`；重投 `replay, applied=False`；重投后终态仍 `confirmed` | ✅ 不重复生效、不覆盖终态 |
| 终态后新 nonce | `terminal_locked, applied=False` | ✅ 终态封闭 |
| 金额不一致(100→1) | 执行 `mismatched`、操作 `human_handoff` | ✅ 转人工 |
| 未知 status(`weird_status`) | `processing, applied=False`，执行保持 `submitted`、操作 `pending` | ✅ 中间态（诚实记录） |
| 跨租户伪造 | `not_found, applied=False`，原记录保持 `submitted` | ✅ 零污染 |

> 诚实说明：任务描述中「金额/状态 unknown → mismatch → HUMAN_HANDOFF」需区分两条路径。
> - **金额不一致**（callback 回调金额 ≠ 记录金额）→ 引擎在 SUBMITTED 上判 `amount_mismatch` 并把**操作**置为 `human_handoff`（确凿证据见上文）。
> - **状态/外部状态 unknown**：在**回调**场景未知 `status` 被归一化为中间态（保持 `submitted`、仅记账 nonce，等待最终回调）；「未知外部状态 → mismatch → 转人工」实际发生在**对账任务**路径（`engine._reconcile_one` 对 `unknown_provider_status` 调 `_reconcile_mismatch` → 执行 `mismatched`、操作 `human_handoff`），两者都被验证。

## 3. 并发/CAS 佐证（pytest）

`tests/test_callback_concurrency_cas.py`（SQLite 后端）：

- `test_same_nonce_concurrent_only_one_applies`：同 nonce 并发 8 路 → 恰好 1 个 confirmed + 7 个 replay；终态唯一。
- `test_diff_nonce_concurrent_converges_to_single_terminal`：异 nonce 并发 8 路 → 恰 1 confirmed + 7 terminal_locked；终态 closed，金额无漂移。
- `test_terminal_status_not_overwritten_by_later_callback`：confirmed 后即使全新 nonce（status=failed）也判 terminal_locked，终态不变。
- `test_cross_tenant_forgery_not_found_zero_pollution`：跨租户伪造 → `not_found`，且**零审计污染**。
- `test_callback_confirm_left_trustworthy_audit_trail`：确认后仅一条可信回调审计（`tenant_id`=执行记录可信租户，`user_id="callback"`）。
- `test_store_atomic_cas_expected_status_only_once`：存储层 `apply_callback_atomic` 的 CAS（`WHERE status=:expected AND status NOT IN (终态)`）并发下只成功一次。
- `test_amount_mismatch_and_illegal_transition_handoff`：金额不一致 → `amount_mismatch` + 操作 `human_handoff`；终态后非法跃迁 → `terminal_locked`。

## 4. HTTP 语义与安全日志边界（`test_callback_security_log.py`）

对外回调端点 `POST /api/callbacks/{channel}`（`src/api/routes.py`）：

| 情形 | HTTP | applied | 写入 |
|---|---|---|---|
| 验签失败 | **401** | — | 仅平台级安全日志（`get_logger("security")`），零租户审计 |
| 未知/跨租户 execution_id | **404** | — | 仅安全日志，零租户审计 |
| 非法 JSON | 200 | False | 仅安全日志 |
| 缺失身份/缺失 nonce | 200 | False | 零租户审计 |
| replay/冲突/中间态/confirmed | 200 | 按 reason | 可信回调审计由 store 同事务写入，端点不重复补 |

- 「不可信/未知」情形（验签失败/非法 JSON/缺失身份或 nonce/未知 execution_id）**只写平台级安全日志**，绝不使用请求 body 的不可信 `tenant_id` 写租户审计或 DB audit——防止伪造审计污染审计链（对齐《Agent 宪法》「绝不信任客户端身份」）。
- 「验签通过且定位到可信执行记录」情形，审计由 `store.apply_callback_atomic` 用**执行记录的可信 `tenant_id`** 在同一事务写入（`user_id="callback"`），端点不再用 body `tenant_id` 追加。

## 5. 复现命令

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_callback_concurrency_cas.py tests/test_callback_security_log.py -q
.\.venv\Scripts\python.exe _verify_tmp\verify_callback_g6.py
```

## 6. 合规备注

- 全程 `shadow`/受控沙箱路径，未触真实资金。
- 回调金额不一致与对账未知状态均 **fail-closed 转人工**（`mismatched` + 操作 `human_handoff`），绝不静默当成功。
- SQLite 并发由单实例共享锁串行化；真实多进程行锁并发出 PostgreSQL `FOR UPDATE` + RLS 验证（`test_pg_callback_concurrency.py`），本报告不重复宣称。
