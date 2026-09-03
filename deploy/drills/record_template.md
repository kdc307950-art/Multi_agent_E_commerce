# 预发布演练记录（模板）

> 每执行一次演练（日例行或上线前）各复制一份，填写真实结果；存于 `deploy/drills/records/`。
> 记录须可复核：时间、操作号、通过项、失败项、关键证据（审计/操作ID）、签发人。

## 基本信息
- 演练编号：`DR-YYYYMMDD-HHMM`
- 环境：`preview`（`after-sales-preview`）
- 执行人 / 复核人：______ / ______
- 开始时间 / 结束时间：______ / ______
- 应用 git 引用：______
- 上线租户白名单：______（首次上线应为少量租户）

## 演练项结果

| # | 演练项 | 操作号/证据ID | 结果 | 时长 | 备注 |
|---|--------|--------------|------|------|------|
| D1 | API/worker/Redis 重启 |  | PASS/FAIL |  | 重启后读接口恢复、在途敏感写未重复 |
| D2 | PostgreSQL 备份恢复 |  | PASS/FAIL |  | RPO≤15min，RTO≤60min，恢复后数据完整 |
| D3 | 审批中断恢复 |  | PASS/FAIL |  | 跨重启可续跑；仅审批后执行 |
| D4 | SSE 断线恢复 |  | PASS/FAIL |  | resume 重放事件，不重复执行 |
| D5 | 并发重复提交 |  | PASS/FAIL |  | 同一 client_request_id 单操作 |
| D6 | 真实业务沙箱对账 |  | PASS/FAIL |  | 非终态收口；不一致转人工 |

## 关键证据（可追溯）
- 用途：`tenant_id` / `session_id` / `approval_id` / `operation_id` 四维可回溯。
- API 侧审计查询：`GET /api/audit?tenant_id=...&target_type=approval&target_id=...`
- Langfuse trace（自托管）：按 `tenant_id` / `session_id` / `environment` 检索。
- 关键操作 ID：______

## 异常与处理
- 出现的异常 / 回滚动作：______
- 值班处理人：______

## 结论
- 是否达到预发布验收：`通过 / 不通过`
- 上线阈值是否满足：`是 / 否`（见 OPS_RUNBOOK §2）
- 签名：______
