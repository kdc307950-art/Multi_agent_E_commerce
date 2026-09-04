# SECURITY_ATTESTATION — 生产合规自证

> **任务**：t3 · 生产合规自证（安全合规评审员 / security）
> **对照标准**：`AGENTS.md`（编码 Agent 工作宪法 · 第一层安全红线 1.1–1.8；业务多智能体系统宪法 · 第二层）
> **自证对象**：`after-sales-prod`（生产栈）**源码 + 测试用例 + 记录** 三重证据
> **日期**：2026-09-04
> **总判定**：**安全红线全部达标（PASS）**；生产放量前置硬条件（受信 CA / 真实租户注入 / 写模型评测）**尚未满足**，当前处于**设计上的 fail-closed 安全默认态**。详见 §8「已知限制 / 放行前置」。

---

## 0. 自证范围与方法

本节自证**不虚构、不降级**，逐条给出【源码路径 · 测试用例 · 记录】三类证据。每条证据均为实际存在的文件/行号/断言；凡"未实测/存在差异"之处在 §8 如实标注。

| 自证项 | 结论 |
|--------|------|
| 1. 生产禁用 `seed_default` | ✅ PASS |
| 2. 登录凭据仅 argon2id/bcrypt PHC | ✅ PASS |
| 3. 租户 RBAC + 跨租户默认拒绝 | ✅ PASS |
| 4. 资金敏感写仅经唯一 `human_approval` | ✅ PASS |
| 5. `EXECUTION_MODE=shadow` 无 live 路径 | ✅ PASS |
| 6. 不记录主观风险标签 | ✅ PASS |

---

## 1. 生产禁用 `seed_default`（`DEMO_SEED_ENABLED=false`），真实租户仅由受控脚本创建

**结论：PASS**

### 1.1 源码证据
- **`src/config.py:31–35`**：`demo_seed_enabled: bool = False`（默认关）。注释明确"仅 development/test 且本值为 true 才允许 `seed_default`；`preview/production` 一律禁止（`is_restricted_env` 为 true 时无论本值如何都拒绝）……首批真实租户与成员必须由受控迁移/运营脚本创建（见 `scripts/create_bootstrapped_tenants.py`），不通过 `seed_default`。"
- **`src/main.py:55–88`** `demo_seed_gate()`：受限环境（`preview/production`）**一律返回 False 不创建演示数据**；若受限环境显式设 `DEMO_SEED_ENABLED=true`，属非法配置，**抛 `RuntimeError` 启动 fail-closed**（`src/main.py:69–79`）。
- **`src/main.py:117–119, 165–166, 228–229`**：`create_app()` 内调用 `demo_seed_gate` 缓存结果；仅 `demo_seed_allowed` 为真时才 `seed_default(store)`。
- **真实租户创建**：`scripts/create_bootstrapped_tenants.py`（受控迁移/运维脚本，非 `seed_default`）。

### 1.2 测试用例
- **`tests/test_seed_gating.py`**：
  - `test_gate_restricted_env_always_skips_regardless_of_request`（production 无论 seed 请求与否 → False）
  - `test_gate_restricted_env_with_demo_enabled_raises_fail_closed`（preview/production + `DEMO_SEED_ENABLED=true` → `RuntimeError`）
  - `test_create_app_preview_demo_enabled_raises_fail_closed`（受限环境 + 显式启用 → 启动即失败）
  - `test_create_app_development_enabled_seeds_demo_tenants` / `test_create_app_development_not_enabled_skips_seed`（仅非受限 + 显式启用才 seed）
- **`tests/test_deploy_checks.py:136–145`** `test_check_secrets_gates_demo_seed_and_config_version`：部署前 `check_secrets` 必须门控 `DEMO_SEED_ENABLED`。

### 1.3 记录
- **`deploy/.env.production:63`**：`DEMO_SEED_ENABLED=false`。
- **`deploy/records/PRODUCTION_DEPLOYMENT_record-20260904-2055.md:56,62`**：`DEMO_SEED_ENABLED=false`；`AUTH_LOGIN_CREDENTIALS={}`（空表，未放行租户，登录一律拒绝并审计）。
- **`deploy/scripts/check_secrets.sh:126–138`**：受限环境 `DEMO_SEED_ENABLED=true` → 拒绝。
- **`deploy/records/CANARY_TENANTS-TEMPLATE.md:27,40`**：`preview/production` 禁用 `seed_default`，首批真实租户由受控脚本创建。

---

## 2. 登录凭据仅 argon2id/bcrypt PHC（`AUTH_CREDENTIAL_HASH`），禁 sha256/md5

**结论：PASS**

### 2.1 源码证据
- **`src/config.py:64–67`**：`auth_credential_hash: str = "argon2id"`（默认，推荐）；合法值为 `argon2id | bcrypt`；"禁止使用无盐 SHA-256（弱哈希）"。`src/config.py:60–63` 注明凭据表只存 PHC 哈希，且明文/哈希永不入日志。
- **`src/auth/security.py:277–324`** `_verify_credential()`：按 `algo`（argon2id | bcrypt）用 PHC 做常量时间校验；**无盐 SHA-256（64hex）/ MD5（32hex）一律拒绝**并返回 `legacy_sha256`/`legacy_md5` 审计原因（`src/auth/security.py:49–52` 定义正则）；未知算法 `fail-closed`（返回 `login_failed`）。
- **`src/auth/security.py:345–388`** `issue_login_token()`：仅 `auth_backend=real` 可登录；凭据按 `auth_credential_hash` 校验；任何失败拒绝并写脱敏审计。
- **`scripts/hash_login_credentials.py`**：生成 PHC 哈希（argon2id/bcrypt），CI/部署时使用。

### 2.2 测试用例
- **`tests/test_credential_hashing.py`**：
  - `test_verify_legacy_sha256_and_md5_always_rejected`（sha256/md5 无论算法一律拒绝并标注 legacy）
  - `test_verify_unknown_algorithm_fails_closed`（`sha256`/`md5`/`weak` → fail-closed）
  - `test_verify_argon2id_correct_and_wrong` / `test_verify_bcrypt_correct_and_wrong`
  - `test_verify_argon2id_rejects_bcrypt_stored_value` / `test_verify_bcrypt_rejects_argon2_stored_value`（不猜测、不降级）
  - `test_redact_detail_replaces_sensitive_keys` / `test_audit_denial_never_records_credential_or_hash`（PHC/凭据/口令永不出现在审计日志）
- **`tests/test_security_regressions.py:367–459`**：`test_login_verifies_argon2id_and_returns_jwt`、`test_login_rejects_legacy_sha256_with_audit`（审计标注 `security.deny.legacy_sha256`）、`test_login_bcrypt_scheme_verifies`、`test_redact_detail_masks_hash_phc_credential_hash_keys`、`test_audit_security_denial_redacts_hash_phc_credential_hash`。

### 2.3 记录
- **`deploy/.env.production:41`**：`AUTH_CREDENTIAL_HASH=argon2id`。
- **`docker-compose.prod.yml:79`**：`AUTH_BACKEND=real`（真实 JWT 后端；受限环境 `AUTH_BACKEND=mock` 启动即失败）。
- **`src/main.py:91–110`** `fail_closed_auth_guard()`：受限环境 `auth_backend != real` 或缺失 `AUTH_JWT_SECRET` → 启动即失败。
- **`deploy/scripts/check_secrets.sh:116–123`**：`AUTH_CREDENTIAL_HASH` 仅允许 argon2id|bcrypt，检测到弱哈希即拒绝。
- **`deploy/records/PRODUCTION_DEPLOYMENT_record-20260904-2055.md:50,64`**：`AUTH_BACKEND=real`、`AUTH_CREDENTIAL_HASH=argon2id`。

---

## 3. 租户成员角色 RBAC + 跨租户默认拒绝（RLS FORCE + `app_runtime` NOBYPASSRLS + 404/403 审计）

**结论：PASS**

### 3.1 源码证据
- **RBAC 角色**：`src/core/types.py:20–29` `Role`（`customer`/`agent`/`admin`/`approver`；`tenant_roles()` 明确"`platform_admin` 不属于此处"）。`src/infrastructure/migrations.py:40–48` `memberships.role CHECK (role IN ('customer','agent','admin','approver'))`。
- **RLS 强制**：`src/infrastructure/migrations.py:254–352` `RLS_TABLES_SQL` 对全部业务表（memberships/sessions/operations/approvals/executions/streams/stream_events/audit/orders/shipping_events/policy_documents/checkpoint_thread_scopes）执行 `ENABLE + FORCE ROW LEVEL SECURITY`，policy `USING (tenant_id = app_current_tenant_id()) WITH CHECK (...)`；`src/infrastructure/migrations.py:354–366` 对官方 checkpoint 表用 `checkpoint_thread_in_current_tenant(thread_id)` 启用 `ENABLE + FORCE RLS`；`src/infrastructure/migrations.py:368–388` 租户作用域函数；`src/infrastructure/migrations.py:390–400` 运行角色 `app_runtime` = `LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS`；`src/infrastructure/migrations.py:500–526` `apply_runtime_role`（仅 GRANT DML，绝不成为 owner）。`tenants` 为平台主数据，按设计**不启用** RLS。
- **服务端身份不可信客户端**：`src/auth/security.py:422–461` `resolve_tenant_context()`（受限环境禁 Mock 认证 fail-closed；`role_mismatch` 拒绝）；`:464–467` `require_role()`（审批仅 `{admin, approver}`）；`:470–520` `validate_session_owner`/`validate_stream_owner`（跨用户 403、不存在 404、过期/删除中拒绝）。
- **314/403 审计**：`src/auth/security.py:75–105` `audit_security_denial()`（reason 机器可读：`cross_tenant`/`forbidden`/`session_not_found`/`approval_access_denied`…）。`src/api/routes.py:378–391` `get_approval` 跨租户 → 404 + 审计 `approval_access_denied`；`:394–430` `decide_approval` `require_role({admin,approver})`，越权 → `forbidden` 审计 + 403，跨租户审批 → 404。

### 3.2 测试用例
- **`tests/test_rls_bypass.py`**：`test_b1_cross_tenant_direct_sql_query_zero_visible`、`test_b2_cross_tenant_direct_sql_write_rejected`、`test_b3_direct_update_tenant_id_rejected`、`test_b4_backup_superuser_bypass_vs_runtime_role_blocked`、`test_b5_app_runtime_cannot_bypass_rls`（`rolbypassrls=false` + FORCE RLS 生效）。
- **`tests/test_security_regressions.py:165–271`**：`test_same_tenant_customer_cannot_list_other_sessions`（403）、`test_same_tenant_customer_cannot_read_other_approval_or_operation`（404）、`test_cross_tenant_order_and_shipping_read_rejected`（404，不泄露存在性）、`test_staff_can_access_same_tenant_other_user_resources`。
- **`tests/test_pg_rls.py`**、**`tests/test_tenant_isolation.py`**、**`tests/test_approval_idempotency.py`**、**`tests/test_execution_engine.py`**（本地 60 例通过，见 §7 记录引用）。

### 3.3 记录
- **`deploy/records/PRODUCTION_DEPLOYMENT_record-20260904-2055.md:69–72`**：全新库迁移（t7）→ 17 张业务表；`app_runtime`(`NO INHERIT, NOBYPASSRLS`)、`backup_role`(`BYPASSRLS` 仅 SELECT 只读)、`migrator`；**15 张表 FORCE RLS + 15 条 `*_tenant_scope` policy**（`tenants` 不启用 RLS）。
- **`evidence/prod-go-live/security-auditor/RLS_ISOLATION_REPORT.md`**：§一 RLS 动态验证 B1–B5 全 PASS（`app_runtime`=NOBYPASSRLS 非 owner；RLS ENABLE+FORCE）；§二 跨租户拒绝 PASS（跨租户读/操作/审批 → 404，越权留痕 `security.deny.approval_access_denied`，联合唯一键隔离同名 order/request id）。
- **`evidence/pg_rls_bypass_probe.json`**、**`evidence/prod-go-live/security-auditor/rls_dynamic_probe.json`**（conclusion=true）、**`deploy/records/RLS_PROD_VERIFICATION.md`**。

---

## 4. 资金敏感写（refund/return/address）仅经唯一 `human_approval` 审批后由 `execute_*` 执行，无 direct 绕过

**结论：PASS**

### 4.1 源码证据
- **唯一审批入口**：`src/graph/approval.py:20–55` `interrupt(payload)` 是唯一 `human_approval`；`validate_resume_params` 要求 `approved` 为 bool，非法即 fail-closed 置 `REJECTED`。
- **图结构无 direct 绕过**：`src/graph/builder.py:1–5,31–52,66–119` —— `process_refund/process_return/update_return_address` 经 `write_approval_condition` → `{"approve":"human_approval", "handle_error":"handle_error"}`；`human_approval` 经 `approval_result_condition` 单一路由到 `execute_refund/execute_return/execute_address_update`；**无任何 `direct → execute_*` 边**。资格/参数/能力校验失败 fail-closed 转 `handle_error`（人工），绝不直接执行。
- **写节点必须设 `needs_approval=True`**：`src/graph/nodes.py:115–197` `_write_action`（能力矩阵门控 → 校验租户/订单 → 资格/金额/地址校验 → 失败转人工 → 创建幂等操作与审批单并置 `needs_approval=True`）。
- **执行节点 5 重复核**：`src/graph/nodes.py:202–289` `_execute` —— ①租户作用域（`get_operation` 跨租户 404）②动作类型匹配当前执行节点 ③操作绑定当前 thread ④**审批状态必须 APPROVED** 且 approval→operation/thread/action 绑定一致 ⑤模型白名单；另幂等重放、`REJECTED/HUMAN_HANDOFF` 拒绝执行。
- **写风险工具门控**：`src/llm/capability.py`、`src/llm/eval/cases.py:16,128`（写操作白名单模型 + 仍须进唯一 `human_approval`）；`src/tools/adapter.py:76`（唯一 human_approval 触发，身份取自 `TenantContext`）；`src/tools/crewai_adapter.py:14`（即使 CrewAI 打开，写操作仍须经唯一 human_approval）。

### 4.2 测试用例
- **`tests/test_approval_idempotency.py`**（拒绝→`REJECTED`、无执行记录、已决策重放幂等不重复执行）。
- **`tests/test_execution_engine.py`**、**`tests/test_e2e_flow.py`**、**`tests/test_callback_security_log.py`**、**`tests/test_sandbox_e2e_flow.py`**。

### 4.3 记录
- **`evidence/prod-go-live/security-auditor/RLS_ISOLATION_REPORT.md`** §三「审批绕过检查」PASS：唯一 `human_approval` interrupt；图无 `direct→execute_*` 边；执行节点 5 重复核；拒绝→REJECTED 无执行；需同租户；`platform_admin` 非租户审批角色。
- **`evidence/E2E_APPROVAL_RECORD.md`**、**`evidence/E2E_APPROVAL_RECORD.json`**、**`deploy/drills/records/approval-security-extra.json`**、**`deploy/drills/records/T4_RACE_REVIEW_submitted_to_su…`**。

---

## 5. `EXECUTION_MODE=shadow`、`provider=mock`、`launch_require_approval=true`、`launch_gate_strict=true`，无 live 执行路径开启

**结论：PASS**

### 5.1 源码证据
- **`src/config.py:91–98`**：`launch_require_approval=True`、`launch_full_audit=True`、`launch_manual_review=True`（默认）；`launch_gate_strict=False`（默认，受限环境上线由脚本 `--strict` 强制，见 `src/config.py:96–98`）。`src/config.py:203–209`：`execution_mode="shadow"`、`execution_provider="mock"`。
- **`src/core/launch_gate.py:25–69`** `verify_launch_gate`：违规项含 `EXECUTION_MODE=live + provider=mock`（禁真实资金）、受限环境缺 `LAUNCH_ALLOWED_TENANTS`、`storage_backend != postgres`、受限环境 `business_data_backend=mock`；`:72–78` `enforce_strict` 违规即抛错 fail-closed。
- **`src/execution/engine.py:71–130`**：`mode` 缺省 `SHADOW`；`:131–158` `_run_shadow` 生成模拟回执**不触真实资金**并立即收敛为 `confirmed`；`:160–170` `_run_live` 必须先有 provider，否则 `503` `"live 执行模式必须配置 FundsProvider（受控执行开关未开启）"`（fail-closed）。
- **`src/execution/provider.py:222–241`** `build_provider`：`mock` → 确定性模拟提供方；`shadow` 无需 provider；`live` 必须显式 provider。

### 5.2 测试用例
- **`tests/test_launch_gate_audit.py`**：`test_launch_gate_ok_when_satisfied_and_strict_passes`（shadow+mock+postgres+三开关+strict → ok）、`test_verify_launch_gate_flags_live_with_mock_provider`（live+mock → 违规）、`test_verify_launch_gate_flags_restricted_without_allowlist`、`test_enforce_strict_raises_on_violation`、`test_launch_allowlist_denies_non_allowlisted_tenant`（403 + 审计 `launch_tenant_not_allowed`）。
- **`tests/test_execution_engine.py`**（shadow 模拟回执）、**`tests/test_deploy_checks.py:136–158`**（`check_secrets` 门控 `LAUNCH_GATE_STRICT`、`EXECUTION_CALLBACK_HMAC_SECRET` 非默认值）。

### 5.3 记录
- **`deploy/.env.production:56–60,67,76,80–82`**：`LAUNCH_GATE_STRICT=true`、`LAUNCH_REQUIRE_APPROVAL=true`、`LAUNCH_FULL_AUDIT=true`、`LAUNCH_MANUAL_REVIEW=true`、`LAUNCH_ALLOWED_TENANTS=__NONE_APPROVED_YET__`、`LLM_BACKEND=mock`、`HIGH_CONFIDENCE_MODELS=__NO_VERIFIED_WRITE_MODEL__`、`EXECUTION_MODE=shadow`、`EXECUTION_PROVIDER=mock`。
- **`deploy/records/PRODUCTION_DEPLOYMENT_record-20260904-2055.md:54–59`**：三开关 + `LAUNCH_GATE_STRICT` 均 `true`；`EXECUTION_MODE=shadow`、`EXECUTION_PROVIDER=mock`（沙箱，不上真实资金）、`LLM_BACKEND=mock`、`HIGH_CONFIDENCE_MODELS=__NO_VERIFIED_WRITE_MODEL__`。
- **`src/execution/engine.py:161–170`** 论证：即使被配置为 live，缺 provider 亦 fail-closed，**不存在静默进入 live 的路径**。`deploy/LLM_GATEWAY_CONFIG.md:6`“`EXECUTION_MODE` 恒为 `shadow`，绝不接真实资金，绝不切 live”。
- **结论**：生产配置 `EXECUTION_MODE=shadow + provider=mock`，且 `launch_gate_strict=true`；`live` + `mock` 会被门控拦截，`live` + 缺 provider 会 fail-closed。**未发现任何 live 执行路径被开启**。

---

## 6. 不记录主观风险标签

**结论：PASS**（未发现主观风险标签机制；仅存客观可验证业务事实）

### 6.1 源码证据（客观事实 + 无主观画像）
- **`src/core/types.py:1–323`**：领域对象全部为客观业务事实（TenantContext/Session/Operation/Approval/Execution/PendingAction…），**无任何风险评分/画像/主观标签字段**。
- **`src/infrastructure/migrations.py:29–352`**：数据面 schema 全部为客观业务表（tenants/memberships/sessions/operations/approvals/executions/streams/audit/orders/shipping_events/policy_documents/checkpoint），**无风险/画像/评级列**。
- **`src/observability/logging.py:24–35`**：结构化上下文键为**有界标识符**（`tenant_id/session_id/thread_id/operation_id/request_id/trace_id`…），敏感键掩码；**无行为画像/主观标签字段**；`src/observability/metrics.py` 亦采用有界标签（`_FORBIDDEN_LABELS` 拒绝高基数 `tenant_id` 直接作标签）。
- **`src/auth/security.py:75–105`** `audit_security_denial`：审计 reason 为**机器可读安全原因**（`cross_tenant/forbidden/role_mismatch/session_not_found/approval_access_denied/legacy_sha256…`），非用户行为评判。

### 6.2 负证（主观关键词全库扫描）
对 `src/` 全量检索 `维权|成瘾|高风险|risk_label|subjective|主观|risk_profile|risky|黑名单用户|用户画像`，**仅命中 1 条**：`src/execution/engine.py:420`，其语义为"超时未确认的执行记录属高风险项，须强制对账/转人工" —— 属**执行/对账技术描述**，非用户行为主观画像。**未发现任何长期记忆/Graphiti/Neo4j 主观画像模块**（代码库当前以检查点持久化 + 交易事实为主，见 `src/graph/state.py`、`src/infrastructure/checkpointer.py`）。

### 6.3 测试与记录
- **`tests/test_security_regressions.py`**（无主观标签断言）、**`tests/test_launch_gate_audit.py`** `test_audit_endpoint_admin_sees_tenant_wide_and_redacted`（仅客观审计 + 脱敏）、**`tests/test_observability.py`**。
- **`evidence/audit_trace.json`**（审计事件均为机器可读 reason + 客观 target，无用户风险评判）。

---

## 7. 汇总：源码 + 测试 + 记录三级证据矩阵

| 自证项 | 源码（主证） | 测试用例 | 记录 |
|--------|------|----------|------|
| 1. 禁 seed_default | `src/config.py:35`、`src/main.py:55–88,117–119` | `tests/test_seed_gating.py` | `deploy/.env.production:63`、`deploy/records/PRODUCTION_DEPLOYMENT_record-20260904-2055.md:56` |
| 2. PHC 凭据 | `src/config.py:64–67`、`src/auth/security.py:277–388` | `tests/test_credential_hashing.py`、`tests/test_security_regressions.py:367–459` | `deploy/.env.production:41`、`docker-compose.prod.yml:79`、`check_secrets.sh:116–123` |
| 3. RBAC + 跨租户拒绝 | `src/core/types.py:20–29`、`src/infrastructure/migrations.py:254–526`、`src/auth/security.py:422–520`、`src/api/routes.py:378–430` | `tests/test_rls_bypass.py`、`tests/test_security_regressions.py:165–271`、`tests/test_tenant_isolation.py`、`tests/test_pg_rls.py` | `deploy/records/PRODUCTION_DEPLOYMENT_record-20260904-2055.md:69–72`、`evidence/…/RLS_ISOLATION_REPORT.md` §一/二、`rls_dynamic_probe.json` |
| 4. 唯一 human_approval | `src/graph/builder.py:66–119`、`src/graph/approval.py:20–55`、`src/graph/nodes.py:202–289`、`src/llm/eval/cases.py:16,128` | `tests/test_approval_idempotency.py`、`tests/test_execution_engine.py`、`tests/test_e2e_flow.py` | `evidence/…/RLS_ISOLATION_REPORT.md` §三、`evidence/E2E_APPROVAL_RECORD.md` |
| 5. shadow 无 live | `src/config.py:91–98,203–209`、`src/core/launch_gate.py:25–78`、`src/execution/engine.py:71–170`、`src/execution/provider.py:222–241` | `tests/test_launch_gate_audit.py`、`tests/test_execution_engine.py`、`tests/test_deploy_checks.py` | `deploy/.env.production:56–60,80–82`、`deploy/records/PRODUCTION_DEPLOYMENT_record-20260904-2055.md:54–59` |
| 6. 不记主观标签 | `src/core/types.py`、`src/infrastructure/migrations.py:29–352`、`src/observability/logging.py:24–35`、`src/auth/security.py:75–105` | `tests/test_security_regressions.py`、`tests/test_launch_gate_audit.py`、`tests/test_observability.py` | `evidence/audit_trace.json` + 全库 grep 负证 |

---

## 8. 已知限制 / 放行前置（如实，非本自证达标项）

以下为**不构成安全隔离红线违反**、但属**生产放量前的硬性前置或待核实项**，必须由对应职能在放行前处理并复核。此为诚实记录，不虚报为"已达标"。

1. **受信 CA（TLS）**：`deploy/records/PRODUCTION_DEPLOYMENT_record-20260904-2055.md:44,91` 标注当前为**自签证书**（CN=preview.local），"**受信 CA 为生产硬前提，当前自签 → BLOCKED**，未达对外可信标准"。→ 归 `release-manager / deploy-engineer`，放行前必须替换为受信 CA 证书。
2. **真实租户/成员未注入**：`deploy/records/PRODUCTION_DEPLOYMENT_record-20260904-2055.md:55,62` `LAUNCH_ALLOWED_TENANTS=__NONE_APPROVED_YET__`、`AUTH_LOGIN_CREDENTIALS={}`（空表）、`HIGH_CONFIDENCE_MODELS=__NO_VERIFIED_WRITE_MODEL__`。当前生产处于**设计上的 fail-closed 安全默认态**（无租户放行、无写模型评测授权、登录一律拒绝）。这与"真实租户仅由受控脚本创建"一致，但**首批真实租户与成员的书面确认/注入尚未发生**。→ 归 `release-manager`。
3. **写模型评测未授权**：`HIGH_CONFIDENCE_MODELS=__NO_VERIFIED_WRITE_MODEL__`，即尚无任何模型通过写操作专项评测（`write_op_pass=true`），因此**所有写操作目前 fail-closed 转人工**（安全但无自动写路径）。→ 归 `llm` 评测（`scripts/evaluate_models.py`、`evidence/llm_candidate_eval.json`）。
4. **迁移/schema 完整性差异（待复核）**：早期 `evidence/prod-go-live/security-auditor/RLS_ISOLATION_REPORT.md` §七 记录一份**可能存在的** `migrations.py::shipping_events` 外键缺陷（单列引用复合主键）可能导致全新库迁移失败；而后续 `deploy/records/PRODUCTION_DEPLOYMENT_record-20260904-2055.md:66–72`（t7）记录**全新库迁移通过**（17 表 + 15 FORCE RLS）。两者存在时间/实现差异。→ 建议 deploy-engineer 在正式生产迁移前**复跑** `migrate_cli` 与数据源验收（`scripts/pg_acceptance_evidence.py`）确认该缺陷已消除、`orders/shipping_events/policy_documents` 齐全。
5. **本自证基于源码 + 测试 + 已采集记录**；`RLS_ISOLATION_REPORT.md` 首段注明其采集时生产栈"尚未落地"，回退 preview 栈取证。`PRODUCTION_DEPLOYMENT_record-20260904-2055.md`（t6/t7/t8）为生产栈**最新**证据，已确认 7 服务 healthy + 全新库迁移通过 + 隔离校验通过。

---

## 9. 结论

对照 `AGENTS.md` 安全红线与验收标准，**六项生产合规自证项全部 PASS**：

1. ✅ 生产禁用 `seed_default`（`DEMO_SEED_ENABLED=false`），真实租户由受控脚本创建；
2. ✅ 登录凭据仅 argon2id/bcrypt PHC（`AUTH_CREDENTIAL_HASH=argon2id`），禁 sha256/md5；
3. ✅ 租户成员角色 RBAC（customer/agent/admin/approver；`platform_admin` 非租户角色），跨租户默认拒绝（RLS `ENABLE+FORCE` + `app_runtime` `NOBYPASSRLS`），跨租户审批/读取 404/403 + 审计留痕；
4. ✅ 资金敏感写（refund/return/address）仅经唯一 `human_approval` 审批后由 `execute_*` 执行，图无 `direct → execute_*` 绕过边，执行节点 5 重复核 + 幂等；
5. ✅ `EXECUTION_MODE=shadow`、`provider=mock`、`launch_require_approval=true`、`launch_gate_strict=true`，未发现任何 live 执行路径开启；
6. ✅ 不记录主观风险标签（仅客观业务事实；全库主观关键词扫描负证通过；日志/指标有界脱敏）。

**安全红线无违反 → Go / No-Go（安全侧）＝ GO。** 但因 §8 所述"受信 CA / 真实租户注入 / 写模型评测 / 迁移复核"四项前置尚未满足，**当前不得向外部租户放量**，应维持 fail-closed 安全默认态直至前置条件关闭。放行前由对应职能闭环并复核本自证的 §8 项。

— *security（安全合规评审员）* · 2026-09-04
