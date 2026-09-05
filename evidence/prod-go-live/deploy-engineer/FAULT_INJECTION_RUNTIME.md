# 故障注入运行态验证（deploy-engineer · 阶段三）

> 位置：**运行态**（真实沙箱网关，`X-Sandbox-Fault` 头注入确定性故障；`SandboxHttpFundsProvider` 收敛为 `ProviderError`）。
> 结论：**4/4 `PASS`（运行态实测）**。网关侧故障被 provider 端收敛为明确的 `ProviderError.code`，进而不静默放行（fail-closed），由引擎转对账/人工。

---

## 判定口径与结果（`runtime/runtime_verify_report.json` → `fault_injection`）

| # | 注入故障 | Provider 收敛 code | 实测 |
|---|---|---|---|
| 1 | `X-Sandbox-Fault: http_500` | `upstream_5xx` | ✅ `http_500: upstream_5xx` |
| 2 | `X-Sandbox-Fault: http_503` | `upstream_5xx` | ✅ `http_503: upstream_5xx` |
| 3 | `X-Sandbox-Fault: timeout`（delay 超过方 deadline） | `timeout` | ✅ `timeout: timeout` |
| 4 | 网关 down（指向不可达端口 `127.0.0.1:1`） | `network` | ✅ `gateway_down: network` |

**网关访问日志佐证**（真实 5xx 由网关返回）：
```
POST /api/sandbox/submit HTTP/1.1" 500 Internal Server Error   # http_500 注入
POST /api/sandbox/submit HTTP/1.1" 503 Service Unavailable    # http_503 注入
```

---

## 运行态之外的引擎层故障收敛（被已有测试覆盖，非本任务伪造）

- **F1/F2 提交超时/5xx 不确定** → `FAILED_UNCERTAIN`（交对账收口，绝不当作成功）；
- **F3 外部明确失败** → `FAILED_DISPATCHED` → 补偿 `COMPENSATED`；
- **F4 补偿失败** → `COMPENSATION_FAILED` → `HUMAN_HANDOFF`；
- **F5 回调重放/重复通知** → 只重放不重复生效；
- **F6 回调签名错误/缺失密钥** → `signature_invalid` 拒绝；
- **F7 网关 down** → 提交不确定 `FAILED_UNCERTAIN`；对账 `mismatch` → 转人工；
- **F8 补偿幂等**（FAILED_DISPATCHED 多次补偿）→ 收敛单一终态，reversal_id 稳定；
- **F9/F10 单执行守卫** → 高并发 execute（live）只 `submit` 一次、绝不重复提交/补偿。

这些由 `tests/test_fault_injection.py`（10 个用例，含并发 64/32）与 `tests/test_concurrency_stress.py` 覆盖（发布基线 `390 passed`）。我本次在**运行态**以真实网关复核了故障注入头（5xx/超时/网关down）的 provider 收敛，作为运行态背书。

---

## 未在本任务做运行态复现的故障（诚实标注：待部署/转由他角色）

- **Redis/Worker/API 重启、PG 断连、Celery 重复投递、checkpoint 恢复失败、审批超时**：依赖 postgres/celery/checkpointer，属 PG/DR 维度（test-runner/security-auditor 的 `pg_*`、`concurrency_stress_pg` 既有证据）；本任务聚焦**网关+执行层**确定性收敛。

---

## 状态：✅ 运行态实测（网关 5xx/超时/down）+ ✅ 引擎层故障收敛既测（`test_fault_injection.py`）
