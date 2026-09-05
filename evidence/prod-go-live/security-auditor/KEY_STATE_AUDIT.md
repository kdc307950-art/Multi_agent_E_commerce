# 密钥轮换状态审计（T0.3 · 不执行真实轮换）

> **归属**：security-auditor · `rc3-prod-readiness`
> **范围**：`deploy/.env.production` 的密钥与 **fail-closed 占位状态** 只读审计，以及"旧测试密钥作废 / 新密钥不进入 Git·镜像·日志"的现状与轮换流程说明。
> **红线**：本文件为**只读审计与流程说明**。全文**未输出任何真实密钥值**；凡涉及已注入的随机机密一律以「已注入（值不展示）」占位，不为审计伪打印、不污染证据。**未执行任何真实轮换。**
> **审计时间**：见 Git 状态与文件指纹（`deploy/.env.production` 共 4172 字节，未修改、未提交）。

---

## 0. 结论速览（TL;DR）

当前 `deploy/.env.production` 处于**设计上的 fail-closed 安全默认态**：

| 断言 | 结论 | 依据 |
|---|---|---|
| 该文件被 `.gitignore` 忽略 | ✅ 是（第 28 行） | `git check-ignore -v` → `.gitignore:28:deploy/.env.production` |
| 该文件未被 Git 跟踪 | ✅ 是 | `git ls-files -- deploy/.env.production` 空；`git log --all` 从未提交；`git status --porcelain --ignored` 显示 `!!`（忽略态） |
| 关键占位值处于 fail-closed 占位态 | ✅ 是 | `AUTH_LOGIN_CREDENTIALS={}`、`LAUNCH_ALLOWED_TENANTS=__NONE_APPROVED_YET__`、`HIGH_CONFIDENCE_MODELS=__NO_VERIFIED_WRITE_MODEL__`、`EXECUTION_MODE=shadow`、`EXECUTION_PROVIDER=mock`、`LLM_BACKEND=mock` |
| 新密钥不进入 Git / 镜像 / 日志 | ✅ 是（机制到位） | gitignore+未跟踪；镜像经 `git archive` 干净上下文构建 & compose 运行期 `${VAR:?}` 注入；日志侧 `LLM_LOG_REDACT=true` + 强凭据不落日志 |
| 旧测试密钥作废 | ✅ 是（不复用/隔离） | prod 与 preview 独立随机密钥、独立库名/卷/网络/备份目录，注释明确「全部随机生成，不复用 preview」 |

**一句话**：生产密钥已独立随机注入 **但尚未放行任何真实租户/成员/写模型** —— 登录、租户门控、写操作执行面全部 fail-closed，属安全默认态；密钥本体的**不进仓库/镜像/日志**机制已验证到位，**待办**仅剩真实租户书面确认 + 登录哈希注入 + 评测写模型授权。

---

## 1. 文件被 gitignore 且未被跟踪（证据）

**1.1 `.gitignore` 第 28 行命中**
```
28: deploy/.env.production
```
（同段第 27 行 `deploy/.env.preview`、第 24–25 行 `.env`/`.env.local`、第 29–30 行 `deploy/secrets/certs/`、`deploy/secrets/preview_login_credentials.txt`、第 76–81 行 `*.key`/`*.pem`/`*.bak`/`*.crt`/`*.csr` 亦一并兜底。）

**1.2 git 运行时验证（本审计实跑，只读）**
```
$ git check-ignore -v deploy/.env.production
.gitignore:28:deploy/.env.production	deploy/.env.production

$ git ls-files -- deploy/.env.production
(空 —— 未被 index 跟踪)

$ git log --oneline --all -- deploy/.env.production
(空 —— 从未被任何提交纳入)

$ git status --porcelain --ignored -- deploy/.env.production deploy/secrets
!! deploy/.env.production
!! deploy/secrets/certs/
!! deploy/secrets/preview_login_credentials.txt
```
**结论**：该文件为**纯忽略、未跟踪、无历史**。跟踪列表中仅存在模板 `deploy/.env.preview.example` 与 `deploy/.env.production.example`（占位、无真实密钥），可提交；真实密钥文件从不入库。

---

## 2. 关键占位值确认（fail-closed 占位态）

以下占位值**并非真实机密**，而是设计上用于「无法上线」的**哨兵 / 空表 / 白名单**,直接引用（供证据口径统一）：

| 变量 | 位置 | 值 | 语义（fail-closed） |
|---|---|---|---|
| `AUTH_LOGIN_CREDENTIALS` | 行 40 | `{}` | 登录凭据为空表 → 无任何用户可登录，`/api/auth/login` 一律拒绝并审计 |
| `LAUNCH_ALLOWED_TENANTS` | 行 60 | `__NONE_APPROVED_YET__` | 无任何放行租户 → 严格门控拒绝所有（`LAUNCH_GATE_STRICT=true`） |
| `HIGH_CONFIDENCE_MODELS` | 行 76 | `__NO_VERIFIED_WRITE_MODEL__` | 无模型通过写评测（`write_op_pass=true`）→ 能力矩阵交集为空 → **所有写操作 fail-closed 转人工** |
| `EXECUTION_MODE` | 行 80 | `shadow` | 首次上线恒为沙箱/只读，不触真实资金 |
| `EXECUTION_PROVIDER` | 行 81 | `mock` | 无真实业务网关/执行提供方；example 中的 `GATEWAY_BASE_URL`/`GATEWAY_API_KEY` 在本文件未配置（不接外部网关） |
| `LLM_BACKEND` | 行 67 | `mock` | 自托管评测端点未达前保持 mock；`LLM_API_KEY` 为 mock 占位 key，不对外发起真实模型调用，未接通真实端点 |

> 一致性佐证：上述哨兵值在**已跟踪文档**中作为证据被引用（非泄露），如 `SECURITY_ATTESTATION.md`、`SURFACE_MATRIX.md`、`PROJECT_STATUS.md`、`deploy/records/PRODUCTION_DEPLOYMENT_record-20260904-2055.md`（行 55/59/92）、`deploy/records/CANARY_TENANTS-20260904-212814.md`（行 21/26），均确认当前生产处于 fail-closed 安全默认态。

---

## 3. 全部密钥 / 机密变量名清单（依 `deploy/.env.production` 逐行枚举）

> **只列变量名**，永不展开值。分类：
> - **A 类·已注入随机机密**（真实密钥，值不展示，仅登记存在与用途）；
> - **B 类·凭据表/轮换池/可观测密钥**（当前为空，等待受控注入）；
> - **C 类·哨兵/白名单**（fail-closed 占位，非机密）。

| # | 变量 | 行 | 类别 | 用途 | 本审计确认的注入态 |
|---|---|---|---|---|---|
| 1 | `POSTGRES_PASSWORD` | 12 | A | 迁移/owner 角色口令 | 已注入（值不展示） |
| 2 | `APP_RUNTIME_PASSWORD` | 17 | A | 运行角色 `app_runtime`（仅 DML）口令 | 已注入（值不展示） |
| 3 | `BACKUP_ROLE_PASSWORD` | 21 | A | 最小权限备份角色口令 | 已注入（值不展示） |
| 4 | `BACKUP_ENC_KEY` | 22 | A | 备份归档对称加密密钥 | 已注入（值不展示） |
| 5 | `REDIS_PASSWORD` | 29 | A | Redis `requirepass` | 已注入（值不展示） |
| 6 | `AUTH_JWT_SECRET` | 32 | A | JWT HS256 签名密钥 | 已注入（值不展示） |
| 7 | `EXECUTION_CALLBACK_HMAC_SECRET` | 82 | A | 回调 HMAC-SHA256 验签密钥 | 已注入（值不展示；非内建默认 `shadow-callback-secret`） |
| 8 | `AUTH_LOGIN_CREDENTIALS` | 40 | B | 登录凭据表 JSON（argon2id/bcrypt PHC 哈希） | 空对象 `{}`（fail-closed） |
| 9 | `AUTH_JWT_ROTATED_SECRETS` | 37 | B | JWT 上一密钥池（轮换用，逗号分隔） | 空 |
| 10 | `AUTH_JWT_KID` | 36 | B | JWT 密钥 ID（kid） | 空 |
| 11 | `LANGFUSE_PUBLIC_KEY` | 85 | B | 自托管可观测公钥 | 空（默认关闭出网） |
| 12 | `LANGFUSE_SECRET_KEY` | 86 | B | 自托管可观测私钥 | 空（默认关闭出网） |
| 13 | `LLM_API_KEY` | 69 | A/C | 自托管 OpenAI 兼容端点密钥 | mock 占位（`LLM_BACKEND=mock`，未接真实端点；值不展示） |
| 14 | `LLM_ALLOWED_HOSTS` | 71 | C | 端点网络白名单 | `model-endpoint,10.0.0.0/8`（受限外联白名单） |
| 15 | `LAUNCH_ALLOWED_TENANTS` | 60 | C | 首批真实租户白名单 | `__NONE_APPROVED_YET__`（fail-closed） |
| 16 | `HIGH_CONFIDENCE_MODELS` | 76 | C | 写操作能力矩阵白名单 | `__NO_VERIFIED_WRITE_MODEL__`（fail-closed） |
| 17 | `EXECUTION_MODE` | 80 | C | 执行面（shadow/live） | `shadow` |
| 18 | `EXECUTION_PROVIDER` | 81 | C | 执行提供方 | `mock`（无真实网关） |

> **待注入的第三方机密（仅存在于模板 `deploy/.env.production.example`，生产文件当前未配置，故不接外部网关）**：`GATEWAY_BASE_URL`、`GATEWAY_API_KEY`（<inject-server-env>）。当前 `EXECUTION_PROVIDER=mock`，未使用。

**下一步（合规预置）**：上述 18 项均无明文/弱哈希凭据；其中 A 类已注入为**独立随机字节**（`openssl rand -hex 32` / `-base64 32`），B/C 类为空或哨兵，满足「无真实租户不可用」的 fail-closed 设计。

---

## 4. 「旧测试密钥作废」现状

**核心机制：生产与 preview/测试完全隔离，旧测试密钥整链作废、不复用。**

1. **独立随机、不复用 preview**：`deploy/.env.production` 头注释明确「全部随机生成，不复用 preview」，且逐项注明独立 `POSTGRES_DB=after_sales_prod`（非 preview 的 `langgraph`）、独立字节随机密钥、独立卷 `./data/prod-*`、独立备份目录、独立网络子网 `172.31.0.0/16`、独立宿主端口 8080/8843。
2. **登录/认证不作废依赖**：认证靠 `AUTH_JWT_SECRET`（已独立注入），登录凭据 `AUTH_LOGIN_CREDENTIALS={}` 为空；`AUTH_CREDENTIAL_HASH=argon2id`（禁弱哈希）。示例/preview 的懒登录凭据**不得**进入生产。
3. **旧测试 LLM/gateway key 作废**：`LLM_BACKEND=mock` + `HIGH_CONFIDENCE_MODELS=__NO_VERIFIED_WRITE_MODEL__`，即旧评测/mock 模型 key 不产生任何写授权；`EXECUTION_PROVIDER=mock` 下 `GATEWAY_API_KEY` 未配置，不接任何旧沙箱网关。
4. **可观测默认关闭出网**：`LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` 为空，配合 `METRICS_EXPOSE_INTERNAL_ONLY=true` + `METRICS_ALLOWED_SOURCES=172.31.0.0/16`，观测链路内网化，无第三方 SaaS 遥测外联（红线：完全自托管 + 禁未审计出网）。
5. **示例租户不得放量**：`deploy/records/CANARY_TENANTS-20260904-212814.md` 明确「`ACME-RETAIL`（示例）→ 生产 `__NONE_APPROVED_YET__`，不向外部放行」。

（注意：这是**状态/机制层面的作废隔离**；本审计**未执行、也不应执行**任何对旧密钥的物理吊销/删除，那属于 release-manager 或运维在真实租户落地时的动作。）

---

## 5. 「新密钥不进入 Git / 镜像 / 日志」现状

### 5.1 不进入 Git —— ✅（已验证）
- `deploy/.env.production` 命中 `.gitignore` 第 28 行，未被跟踪、无提交历史（见 §1 证据）。
- 可提交的仅为 `deploy/.env.production.example`（占位符 `<inject-*>`/`<openssl ...>`/`{}`，无真实密钥）。
- 备份/密钥产物亦被兜底忽略：`deploy/backups/`、`*.dump`/`*.dump.enc`/`*.sha256`/`*.manifest.csv`、`*.key`/`*.pem`/`*.crt` 等。

### 5.2 不进入镜像 —— ✅（机制到位）
- 镜像经 `deploy/scripts/deploy.sh`/`common.sh build_from_worktree` 用 **`git archive <ref> | tar -x`** 物化**已提交的受控文件**为 build context —— 未跟踪的 `deploy/.env.production` **天然不进入构建上下文**，秘钥不 baked 进镜像层。
- 运行期经 `docker compose --env-file deploy/.env.production` 注入，镜像内无密钥：`docker-compose.prod.yml` 使用 `${POSTGRES_PASSWORD:?必由密钥注入}` 等 **required 替换语法**，缺失即 compose `config`/`up` 失败（fail-closed）；`DATABASE_MIGRATOR_URL`、Redis `--requirepass`、`APP_RUNTIME_PASSWORD`、`BACKUP_ROLE_PASSWORD` 等均由 env-file 运行期替换，不写入镜像层。
- 与发布记录 `/deploy/records/PRODUCTION_DEPLOYMENT_record-20260904-2055.md` 的「干净 worktree 构建」口径一致（仅该引用下已提交源码进 build context）。

### 5.3 不进入日志 —— ✅（机制到位）
- `LLM_LOG_REDACT=true`：LLM 相关请求/响应侧脱敏。
- 强凭据不落日志：`DR_KEY_MANAGEMENT.md` 明确「解密密钥只经 `BACKUP_ENC_KEY` 环境变量注入；脚本绝不 `echo` 它，也不把 URL/password 写入日志」；`pg_dump` 明文经管道直达 `openssl enc` 加密，磁盘仅存密文 `.dump.enc`+校验和+manifest，临时明文用完即删。
- `check_secrets.sh` 只**校验存在/非占位/非弱哈希**，从不打印密钥值；明文凭据文件 `deploy/secrets/preview_login_credentials.txt` 被发现含明文即被拒（fail-closed）。
- 观测内网化 + 有界标签（`component="pg_backup"`，不含租户/敏感维度）。

---

## 6. 轮换流程（引用 `check_secrets.sh` / `.env.production.example`，本审计不执行）

> 所有轮换按「生成随机新值 → 注入 env-file（服务器环境，chmod 600）→ `check_secrets.sh` fail-closed 校验 → 重新部署 →（涉及备份/令牌的）保留旧值窗口并验证」。以下为**委托给 release-manager / 运维**的执行前置说明。

### 6.1 通用注入与 fail-closed 校验链
- 生成：`openssl rand -hex 32`（口令/密钥）或 `openssl rand -base64 32`（`BACKUP_ENC_KEY`）。
- 写入 `deploy/.env.production`（gitignored），`chmod 600`。
- 校验：`bash deploy/scripts/check_secrets.sh` —— `REQUIRED` 清单（§3 中 A 类 + `LLM_ALLOWED_HOSTS`/`HIGH_CONFIDENCE_MODELS`/`LLM_EVAL_REPORT_PATH`/`LAUNCH_ALLOWED_TENANTS`/`EXECUTION_CALLBACK_HMAC_SECRET`）逐项**非空、非占位（`is_placeholder` 检测 `<`/`>`）、且 hits 被禁默认值即失败**；`LAUNCH_GATE_STRICT` 必须为 `true`；`AUTH_CREDENTIAL_HASH` 仅 `argon2id|bcrypt`；`DEMO_SEED_ENABLED=false`；`EXECUTION_MODE=shadow`（live 且 provider=mock 则拒绝）。任一失败 → `die`（fail-closed，拒绝部署）。
- 重新部署：`docker compose --env-file deploy/.env.production -f docker-compose.prod.yml up -d`（`config` 先验）。

### 6.2 JWT 密钥轮换（`AUTH_JWT_SECRET` / `AUTH_JWT_KID` / `AUTH_JWT_ROTATED_SECRETS`）
- 参照 `deploy/PREVIEW_DEPLOYMENT_CHECKLIST.md` 行 49：轮换时把**上一条密钥**写入 `AUTH_JWT_ROTATED_SECRETS`（逗号分隔的轮换池），用于**仅校验旧令牌**；未知 `kid` 一律拒绝。新密钥写入 `AUTH_JWT_SECRET` 并更新 `AUTH_JWT_KID`。
- 由于当前 `AUTH_LOGIN_CREDENTIALS={}`（无用户可登录），业务侧暂无存量令牌需平滑，可安全切换新密钥。

### 6.3 登录凭据轮换（`AUTH_LOGIN_CREDENTIALS` / `AUTH_CREDENTIAL_HASH`）
- 生成：`python scripts/hash_login_credentials.py --algorithm argon2id`（或 bcrypt）为每位**书面确认成员**生成 PHC 哈希。
- 注入：作为 JSON 写入 `AUTH_LOGIN_CREDENTIALS`（键 `<tenant_id>:<user_id>` → PHC 哈希）。
- 校验：`check_secrets.sh` 优先调用 `hash_login_credentials.py --check-env`；缺该能力时用本地指纹拒绝 64-bit hex（sha256 弱哈希）及非 argon2id/bcrypt 算法。
- 仅当 `LAUNCH_ALLOWED_TENANTS` 已含该租户时凭据才可登录；跨租户/未放行租户一律拒绝并审计。

### 6.4 备份密钥轮换（`BACKUP_ENC_KEY` / `BACKUP_ROLE_PASSWORD`）
- 参照 `deploy/DR_KEY_MANAGEMENT.md` 行 41–43：更新 `BACKUP_ENC_KEY` 后**旧归档将无法解密**，须在窗口内**保留旧密钥**（并存于受控 vault，且密钥与归档**异机**），并对全部在营归档**重新加密或用新密钥重打一次全量备份**；建议最长 **90 天**轮换一次，每次轮换**触发一次恢复演练**（验证 RPO/RTO + `sha256sum` 复核 + 密文可解）。
- 泄露处置：任何字面量泄露 → **立即轮换并重打加密备份**；如密钥与归档同机残留，迁移归档到受控异机。

### 6.5 LLM 端点/写模型授权（`LLM_BACKEND` / `LLM_API_KEY` / `LLM_ALLOWED_HOSTS` / `HIGH_CONFIDENCE_MODELS`）
- 真实自托管端点（vLLM/Ollama）确认后：`LLM_BACKEND=openai_compatible` + 注入真实 `LLM_API_KEY` + `LLM_ALLOWED_HOSTS` 收敛到已批准内网段；随后**重新运行 `scripts/evaluate_models.py` 更新 `evidence/llm_candidate_eval.json`**，仅 `write_op_pass=true` 的模型写入 `HIGH_CONFIDENCE_MODELS`（否则保持 `__NO_VERIFIED_WRITE_MODEL__` fail-closed）。
- 达标前不得从 mock 直写资金；写路径永远叠加人工审批（`human_approval`）。

### 6.6 租户放行（`LAUNCH_ALLOWED_TENANTS`）
- 仅当**首批真实租户经书面确认**后，由 release-manager 将确认租户 ID 注入 `LAUNCH_ALLOWED_TENANTS`（替换哨兵 `__NONE_APPROVED_YET__`）；`LAUNCH_GATE_STRICT=true` + `verify_launch_gate --strict` 要求非空。未确认前保持 fail-closed。

---

## 7. 安全审计发现与结论

### 7.1 ✅ 通过（无密钥泄露/伪造证据）
- 密钥文件**不入 Git、无历史、无泄露**；镜像运行期注入不 baked；日志/观测**脱敏且内网化**。**防伪造基线成立**：`EXECUTION_CALLBACK_HMAC_SECRET` 非内建默认，回调验签有效；`AUTH_JWT_SECRET` 独立注入；写路径无人可写（`HIGH_CONFIDENCE_MODELS` 空集）。
- fail-closed 三要素齐备：**无人可登录**（凭据空表）、**无租户放行**（`__NONE_APPROVED_YET__`）、**无人可写**（`__NO_VERIFIED_WRITE_MODEL__`），与《代理宪法》安全红线一致。

### 7.2 ⚠️ 待办（跨角色，不属本次审计执行；提交 captain 归结）
1. **真实租户书面确认 + `LAUNCH_ALLOWED_TENANTS` 注入**（release-manager / 业务方；当前 `__NONE_APPROVED_YET__`）。
2. **首批成员 argon2id PHC 注入 `AUTH_LOGIN_CREDENTIALS`**（release-manager；当前 `{}`）。
3. **写模型专项评测并授权 `HIGH_CONFIDENCE_MODELS`**（llm 评测；当前 `__NO_VERIFIED_WRITE_MODEL__`，所有写转人工）。
4. （运维）LLM 网关真实端点 `LLM_API_KEY`/`LLM_ALLOWED_HOSTS` 收敛；`EXECUTION_PROVIDER` 由 `mock` 按需切 `sandbox_http` 前先配 `GATEWAY_API_KEY`/`GATEWAY_BASE_URL`。

### 7.3 审计局限性
- 本审计为**本地文件系统 + Git 只读核验**，**未实际运行**生产编排/容器、未联机验证 compose 启动、未对服务器上的真实密钥做轮换/吊销。**若需服务器端实考（`--env-file` 实际注入、`check_secrets.sh` 实跑、`docker compose config` 实跑）**，属 deploy-engineer / 运维职责，本审计不据此断言「部署侧已实测」。

---

*证据：`git check-ignore`、`git ls-files`、`git log`、`git status --porcelain --ignored`（均只读）。审计仅引用占位/哨兵值与变量名，未展示任何真实机密。*
