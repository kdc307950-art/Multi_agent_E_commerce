# 历史证据索引

本目录保留发布过程中的原始验收记录。旧报告中的结论只代表其文件名日期和对应提交时点，不能覆盖当前状态。

## 当前权威入口

- 当前项目状态：`PROJECT_STATUS.md`
- 当前项目说明：`README.md`
- 当前自动化汇总：`evidence/acceptance_report.json`
- 当前 Milvus/BGE 证据：`evidence/MILVUS_BGE_LOCAL_ACCEPTANCE.md`
- 当前 Milvus → Agentic RAG 证据：`evidence/MILVUS_RAG_INTEGRATION.md`
- 当前真实 CrewAI/Qwen3:8B 写工具证据：`evidence/llm_candidate_eval.json` 及其同目录验收报告

## 已被后续证据取代的快照

以下文件仍保留用于审计追溯，但其中“真实模型未接入”“CrewAI 真实链路 BLOCKED”“416/396 passed”等表述属于早期快照：

- `evidence/prod-go-live/acceptance-engineer/LLM_GATEWAY_ACCEPTANCE.md`
- `evidence/prod-go-live/acceptance-engineer/CREWAI_MAINPATH_VERIFY.md`
- `evidence/prod-go-live/release-manager/RC3_PROD_READINESS_MASTER_REPORT.md`
- `evidence/prod-go-live/release-manager/RC4_PROD_READINESS_MASTER_REPORT.md`
- `evidence/prod-go-live/test-runner/TEST_EVIDENCE_RC4.md`
- `evidence/prod-go-live/test-runner/TEST_EVIDENCE_WORKTREE_20260905.md`

## 仍然有效的边界

历史报告被新证据取代，不代表生产放量已获批准。真实租户、受信 TLS、真实资金渠道、生产网关、7 天观察和生产级写模型白名单仍按当前 `PROJECT_STATUS.md` 标注为未满足或需外部复验；正式生产继续 `NO-GO`。
