# 回调安全运行态验证（deploy-engineer · 阶段三）

> 位置：运行态（真实沙箱网关提交出 `external_txn_id`，再以 `ExecutionEngine.apply_callback` 对回调做**验签 + 重放保护 + 终态封闭 + 原子应用**）。
> 结论：**全部 `PASS`（运行态实测）**。回调链路绝不因坏签名/重放/跨租户/篡改金额而误确认或重复扣款。

---

## 判定口径与结果（`runtime/runtime_verify_report.json` → `callback_security`）

| # | 攻击/异常场景 | 期望收敛 | 实测 |
|---|---|---|---|
| 1 | 正确 HMAC 回调 | `applied=True, status=confirmed` | ✅ `correct_hmac_confirmed: true` |
| 2 | **错误 HMAC**（坏签名） | `signature_invalid`，不确认 | ✅ `wrong_hmac_signature_invalid: true` |
| 3 | **同 nonce 重复回调（重放）** | `replay`，只重放不重复生效 | ✅ `replay_same_nonce_replay: true` |
| 4 | **终态后新 nonce** | `terminal_locked` 拒绝（防重复扣款/退款） | ✅ `terminal_locked_new_nonce: true` |
| 5 | **未知 execution_id**（合法签名伪造） | `not_found` | ✅ `unknown_execution_not_found: true` |
| 6 | **跨租户回调**（body tenant=B 携 A 的执行记录） | `not_found`，不泄露存在性 | ✅ `cross_tenant_callback_not_found: true` |
| 7 | **篡改金额**（合法签名但 amount=999999 与实际 299 不符） | `amount_mismatch` 转人工，不记账 | ✅ `tampered_amount_amount_mismatch: true` |

---

## 设计要点（代码证据，非本任务新增）

- **验签**：`verify_hmac_signature(callback_secret, raw_body, signature)`；密钥由 `EXECUTION_CALLBACK_HMAC_SECRET` 注入，禁止默认值（compose `:?` fail-closed）。
- **非重放 + 终态封闭**：`store.apply_callback_atomic` 在同一事务内完成 nonce 记账 + 状态 CAS + 操作更新 + 权威审计（用执行记录的可信 `tenant_id`，**绝不**用 body 的不可信 tenant）。同 nonce→replay；终态+新 nonce→terminal_locked；金额不符→amount_mismatch。
- **审计只在真正发生状态跃迁时写**（`execution.callback.confirmed`），由 `user_id="callback"`、可信 tenant 落审计；重放/终态封闭/中间态不产生额外回调审计。
- **平台安全日志**：验签失败/非法 JSON/未知 execution/缺失身份或 nonce 只写 `get_logger("security")`（channel/client_ip/reason），**绝不用 body 不可信 tenant 写租户审计**（见 `tests/test_callback_security_log.py`）。

> 说明：「回调**时间戳过期**」本系统以 **nonce 重放保护 + 终态封闭** 实现（不依赖单调时间戳窗口）；HTTP 语义：signature_invalid→401、not_found→404、其余→200（applied=False），确认→200（applied=True），`test_callback_security_log.py` 已覆盖。

---

## 状态：✅ 运行态实测（引擎+内存store 状态机收敛语义）+ ✅ 端点安全日志既测（`test_callback_security_log.py`）
