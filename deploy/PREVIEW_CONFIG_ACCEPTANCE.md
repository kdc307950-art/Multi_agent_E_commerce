# Preview 配置验收记录（Config Acceptance Record）

> 复核对象：本阶段对 preview 部署面（`docker-compose.preview.yml`、`deploy/.env.preview.example`、
> `deploy/scripts/check_secrets.sh`、`deploy/scripts/deploy.sh`）的"补齐认证 / LLM 白名单 / 模型能力矩阵 /
> 评测报告路径 / 首批租户 / 执行模式 / 执行提供方 / 回调 HMAC 密钥"加固验收。
> 验收口径：安全先于便利，受限环境（preview/production）fail-closed，任何缺项不通过即拒绝部署。

---

## 1. 补齐的配置面（对照任务清单）

| 配置面 | 环境变量 | 本阶段处理 |
|---|---|---|
| 认证 | `AUTH_JWT_SECRET` / `AUTH_JWT_ISSUER` / `AUTH_JWT_AUDIENCE` / `AUTH_JWT_TTL_SECONDS` / `AUTH_JWT_KID` / `AUTH_JWT_ROTATED_SECRETS` / `AUTH_LOGIN_CREDENTIALS` | 全量透传进 `api` 容器；`AUTH_JWT_SECRET` 与 `AUTH_LOGIN_CREDENTIALS` 用 `:?` 强制注入；缺 JWT → api 启动 fail-closed |
| LLM 白名单 | `LLM_ALLOWED_HOSTS` | 透传进 `api`；受限环境 `:?` 强制，空则端点访问 fail-closed（阻断未批准外联） |
| 模型能力矩阵 | `HIGH_CONFIDENCE_MODELS` | `:?` 强制；仅白名单内且评测 `write_op_pass=true` 的模型可写，其余转人工 |
| 评测报告路径 | `LLM_EVAL_REPORT_PATH` | 默认 `/app/evidence/llm_candidate_eval.json`；compose 将宿主 `evidence/llm_candidate_eval.json` 只读挂载进 `api` 容器 |
| 首批租户 | `LAUNCH_ALLOWED_TENANTS` | `:?` 强制；`verify_launch_gate --strict` 要求非空（首次上线限定少量租户） |
| 执行模式 | `EXECUTION_MODE` | 默认 `shadow`；`live`+`mock` 提供方 → 门控失败 |
| 执行提供方 | `EXECUTION_PROVIDER` | 默认 `mock`（沙箱）；`live` 时禁止 `mock` |
| 回调 HMAC 密钥 | `EXECUTION_CALLBACK_HMAC_SECRET` | `:?` 强制，且**禁止内建默认 `shadow-callback-secret`**（`check_secrets` 拒绝）；`api`/`worker` 均注入 |
| 严格上线门控 | `LAUNCH_GATE_STRICT` | 受限环境强制 `true`（compose 固定 `true` + `check_secrets` 校验） |

---

## 2. deploy.sh 强制闸门顺序（任一失败即非零退出）

部署前三道闸门依次通过后才允许构建/启动：

```
[1/7] 密钥检查       → check_secrets.sh                     （缺机密/占位/命中默认回调密钥/STRICT 非 true 即拒）
[2/7] 严格上线门控   → verify_launch_gate.py --strict        （首批租户非空、shadow 沙箱、仅审批后执行、全量审计、人工复核、PostgreSQL）
[3/7] Compose 配置   → docker compose config --quiet         （${VAR:?} 必需变量与拓扑合法性）
[4/7] 生成 TLS 证书  → gen_certs.sh
[5/7] 构建镜像       → docker compose build --pull
[6/7] fail-closed 验证 → verify_fail_closed.sh               （受限环境 Mock/缺密钥 启动即失败）
[7/7] 启动服务       → docker compose up -d → wait nginx healthy → 记录 git 引用
```

`check_secrets.sh`、`deploy.sh`、`common.sh` 均为 `set -euo pipefail`；`deploy.sh` 中任何一道闸门非零即
`exit 1`，不会带故障启动。

---

## 3. 复核结果（本机可执行子集已实际运行）

在仓库根目录、使用项目 venv（Python 3.12.9）运行：

| 验收项 | 命令 | 结果 |
|---|---|---|
| check_secrets 通过（真实注入值） | `bash deploy/scripts/check_secrets.sh`（ENV_FILE 指向注入完毕的 `.env.preview`） | ✅ `机密检查通过`（EXIT=0） |
| check_secrets：缺 JWT | 删除 `AUTH_JWT_SECRET` / 置 `AUTH_JWT_SECRET=` | ✅ 拒绝（EXIT=1） |
| check_secrets：缺模型白名单 | `HIGH_CONFIDENCE_MODELS=<inject>` | ✅ 拒绝（EXIT=1） |
| check_secrets：缺回调密钥 | 删除 `EXECUTION_CALLBACK_HMAC_SECRET` | ✅ 拒绝（EXIT=1） |
| check_secrets：命中默认回调密钥 | `EXECUTION_CALLBACK_HMAC_SECRET=shadow-callback-secret` | ✅ 拒绝（EXIT=1） |
| check_secrets：`LAUNCH_GATE_STRICT` 非 true（受限） | 删除 `LAUNCH_GATE_STRICT` | ✅ 拒绝（EXIT=1） |
| 严格上线门控通过 | `python scripts/verify_launch_gate.py --strict`（首批租户非空、shadow） | ✅ `[PASS]`（EXIT=0） |
| 严格上线门控失败 | 同上，`LAUNCH_ALLOWED_TENANTS=` 为空 | ✅ `[FAIL]`（EXIT=1，违规项：首批租户白名单为空） |
| 受限环境 fail-closed | `create_app(env=preview, auth_backend=mock)` / `(env=preview, real, secret="")` → 抛 `RuntimeError`；`(preview, real, secret="s3cret")` → 正常构建 | ✅ `FAIL_CLOSED_VERIFY: OK` |

**本机能跑的脚本级闸门全部通过/按预期 fail-closed。** 以下三项属目标服务器 Docker 环境，已作为确定性脚本交付，
需在目标服务器按下述方式复跑（本会话无 Docker，未实机运行）：

- `bash deploy/scripts/deploy.sh`（内部按 §2 顺序依次执行四道闸门）。
- `docker compose --env-file deploy/.env.preview -f docker-compose.preview.yml config --quiet`（Compose 配置检查）。
- `bash deploy/scripts/verify_fail_closed.sh`（在一次性 `api` 容器内验证 fail-closed）。

---

## 4. 验收状态

- ✅ 安全版 Preview Compose：`docker-compose.preview.yml` 已补齐认证/LLM 白名单/能力矩阵/评测报告挂载/
  首批租户/执行面/回调 HMAC，全部走密钥注入，无演示密码；`api`/`worker` 注入回调密钥，`api` 只读挂载评测报告。
- ✅ 真实 `.env.preview` 注入清单：`deploy/.env.preview.example` 已补齐并标注占位符与禁止默认值说明；
  复制为 `deploy/.env.preview`（已 gitignore）逐项替换即可。
- ✅ 严格门控部署脚本：`deploy.sh` 依次执行密钥检查 → 严格上线门控 → Compose 配置检查 → fail-closed 验证，
  任一失败即非零退出；`check_secrets.sh` 覆盖缺 JWT/缺模型白名单/缺回调密钥/命中默认回调密钥/STRICT 非 true。
- ✅ 配置验收记录：本文件。

**完成标准核对**：`check_secrets.sh` 通过（✔）；`verify_launch_gate.py --strict` 通过（✔，在首批租户非空 +
shadow 沙箱 + 全量审计/人工复核下）；缺 JWT、缺模型白名单、缺回调密钥时部署失败（✔，见 §3 拒绝场景）。

> 注：本会话环境无 Docker、无 SSH 主机；Docker 实机部分以确定性脚本 + 本记录交付，需按 §2/§3 在目标服务器复跑。
