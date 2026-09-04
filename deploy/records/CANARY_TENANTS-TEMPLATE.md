# 灰度租户清单（模板骨架）

> **填表说明**：模板/骨架，字段占位 `<...>`。首批上线租户与成员角色来自 **t1（部署 + 上线门控配置）**，
> 成员角色/归属与合规来自 **t2（安全评审）**；放量阶梯为**渐进放量设计**，最终放量须由放量审批单（见
> `deploy/records/SCALEUP_APPROVAL-TEMPLATE.md`）批准后执行（t7 汇总）。
> 最终文件名约定：`deploy/records/CANARY_TENANTS-<ts>.md`。

---

## 0. 记录标识
- 记录 ID：`CANARY-<ts>`
- 生成时间戳 `<ts>`：______
- 维护人 / 复核人：`<t1 部署人> / <t2 安全评审人>`
- 依据：`LAUNCH_ALLOWED_TENANTS`（`deploy/.env.preview`）+ **受控迁移/运维脚本**创建首批真实租户与成员（`preview/production` 禁用 `seed_default`）+ `AUTH_LOGIN_CREDENTIALS`（argon2id/bcrypt PHC）

---

## 1. 首批上线租户白名单（= 上线门控允许集合）
| 租户 ID | 是否在 `LAUNCH_ALLOWED_TENANTS` | 数据面是否已由受控迁移/运维创建 | 状态/准入 | 回填来源 |
|---------|-------------------------------|--------------------------------|-----------|---------|
| `TENANT-A` | `<是/否>`（`<t1 从 env 实测>`） | `<是/否>` | `<启用/停用/未知>` | t1（确认）、t2（合规） |
| `TENANT-B` | `<是/否>` | `<是/否>` | `<启用/停用/未知>` | t1（确认）、t2（合规） |
| `<其它候选>` | `<是/否>` | `<是/否>` | `<...>` | t1/t2 |

## 2. 成员角色与归属（服务端 `TenantContext` 认证后取，禁止信任客户端）
> 角色仅限 `customer` / `agent` / `admin` / `approver`；`platform_admin` 为独立受控能力，不作租户成员角色。
> 登录凭据来自 `AUTH_LOGIN_CREDENTIALS`（JSON：`{"<tenant_id>:<user_id>": "<PHC 哈希（argon2id|bcrypt）>"}`），由 t1/t2 核验；`AUTH_CREDENTIAL_HASH` 仅允许 argon2id|bcrypt。

| 租户 | 成员 user_id | 角色 | 是否可审批（admin/approver） | 凭据注入 |
|------|-------------|------|------------------------------|----------|
| `TENANT-A` | `<user>` | `customer` | 否 | `<已注入/未注入>` |
| `TENANT-A` | `<user>` | `agent` | 否 | `<...>` |
| `TENANT-A` | `<user>` | `admin` | **是** | `<...>` |
| `TENANT-A` | `<user>` | `approver` | **是** | `<...>` |
| `TENANT-B` | `<user>` | `customer` | 否 | `<...>` |
| `TENANT-B` | `<user>` | `agent` | 否 | `<...>` |
| `TENANT-B` | `<user>` | `admin` | **是** | `<...>` |
| `TENANT-B` | `<user>` | `approver` | **是** | `<...>` |

> 说明：**`preview/production` 禁用 `seed_default`**（`DEMO_SEED_ENABLED=false`），首批真实租户与成员必须由
> **受控迁移/运维脚本**创建（如 `scripts/create_bootstrapped_tenants.py`），再为其成员生成 argon2id/bcrypt PHC
> 填入 `AUTH_LOGIN_CREDENTIALS`。**具体 user_id 与是否已注入登录凭据**由 t1/t2 实跑确认后回填；未确认前不得当作已配置。

## 3. 放量阶梯（渐进上线设计）
| 阶梯 | 租户范围 | 放量动作 | 通过条件（阈值+演练） | 审批单引用 |
|------|---------|----------|----------------------|-----------|
| Tier 0（内部验收） | `TENANT-A`（首个租户，最小面） | 仅启用 1 个租户 | `healthcheck.sh` 全过 + DR 演练可复核 + RPO≤15min/RTO≤60min | `<SCALEUP_APPROVAL-<ts>>` |
| Tier 1（灰度） | `TENANT-A` + `TENANT-B` | 扩到 2 个租户 | 上线阈值全部 + 无重复副作用 + 指标正常 | `<...>` |
| Tier 2（放量 50%） | `<目标租户区间>` | 逐步扩量 | 告警无异常 + 人工介入率/错误率达标 | `<...>` |
| Tier 3（全量） | `<全部目标租户>` | 全量 | 稳定期观察 + 二次审批 | `<...>` |
- 当前所处阶梯：`<待 t1/t3 观测后回填>`；放量依据《OPS_RUNBOOK §2 上线阈值》《§3 回滚条件》。

## 4. 放量审批单引用
- 本阶梯放量审批单：`deploy/records/SCALEUP_APPROVAL-<ts>.md` —— `<已创建/未创建>`（t7 汇总）
- 放量前条件（阈值达成 + 演练记录可复核）：`<待回填，见 OPS_RUNBOOK §2>`（t3 演练指标 + t6 DR 实跑）
- 审批人（租户 `admin`/`approver`）：`<t2 确认>`；复核人：`<...>` —— **同租户审批，禁止跨租户**

## 5. 风险与备注
- 跨租户风险：`<待 t2 安全评审回填>`（跨租户访问默认拒绝；审批/操作须同租户）
- 停用租户 / 成员失效：`<t2 确认处理，如停用即阻断新请求与后台任务>`
- 记忆/知识/向量/图谱按租户隔离核查：`<待 t2 回填>`（Graphiti/Milvus 本阶段非前置）
- 放量/停用须留痕（操作者、审批依据、时间、结果）：`<t2/t7>`

## 结论（待回填，勿臆造）
- 首批租户是否已就绪、成员角色正确：`<是/否>`（未实机核验不得填 "是"）
- 是否允许进入 Tier 0/1：`<是/否>`（须放量审批单批准）
- 签发：______
