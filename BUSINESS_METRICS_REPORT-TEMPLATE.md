# 业务指标报告（BUSINESS METRICS REPORT）—— 团队工作区副本

> **单源说明**：本文件为团队工作区根目录副本，内容与 `deploy/observability/BUSINESS_METRICS_REPORT-TEMPLATE.md` 一致（改一处必改两处）。
> 运维侧以 `deploy/observability/` 版本为基准。

**命名**：`BUSINESS_METRICS_REPORT-<period>.md`，`<period>` 用周/日标识（如 `BUSINESS_METRICS_REPORT-WEEK-20260907.md`）。
**地位**：周期（建议每周一次）汇总**客观业务指标**，与《DAILY_OBSERVATION》配套；留存在 `deploy/observability/` 或 `deploy/records/`。
**口径纪律**：只写客观指标（解决率/人工介入率/AHT/忠实度/相关性/延迟/token），按真实口径统计，不虚报（《代理宪法 第二层 §7.4》）；
租户明细走受控审计（`GET /api/audit`），**绝不用高基数 `tenant_id` 作 Prometheus 标签**。

完整字段结构（§1 核心业务指标、§2 资金/执行链路、§3 安全合规、§4 观测质量、§5 异常结论）见 `deploy/observability/BUSINESS_METRICS_REPORT-TEMPLATE.md`。此处不重复整表。
