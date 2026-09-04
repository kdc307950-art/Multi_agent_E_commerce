# 每日观察记录（DAILY OBSERVATION）—— 团队工作区副本

> **单源说明**：本文件为团队工作区根目录副本，内容与 `deploy/observability/DAILY_OBSERVATION-TEMPLATE.md` 一致（改一处必改两处）。
> 运维侧执行时以 `deploy/observability/DAILY_OBSERVATION-TEMPLATE.md` 为基准。

**命名**：`DAILY_OBSERVATION-<date>.md`，`<date>` 用 `YYYYMMDD`（如 `DAILY_OBSERVATION-20260907.md`）。
**地位**：每天按 `DAILY_CHECKS_RUNBOOK.md` §0 执行 8 项检查后，填入并留存在 `deploy/observability/` 或 `deploy/records/`。
**口径**：只写客观事实，不写主观画像；敏感数据（地址/支付/订单号）不入明文；租户明细走 `GET /api/audit`，绝不用 `tenant_id` 作 Prometheus 标签。

完整字段结构见 `deploy/observability/DAILY_OBSERVATION-TEMPLATE.md`（§0 元信息、§1–§8 八项检查、§9 告警命中、§10 处置留痕）。此处不重复整表。
