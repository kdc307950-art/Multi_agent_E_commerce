#!/usr/bin/env bash
# check_secrets.sh —— 校验 preview 必需的服务器机密已注入，缺失/占位/命中被禁默认值即失败（fail-closed）。
# 覆盖：认证、LLM 端点白名单、模型能力矩阵、评测报告、首批租户、执行模式/提供方、回调 HMAC 密钥、
#       严格上线门控（LAUNCH_GATE_STRICT=true）。
# 用法：bash deploy/scripts/check_secrets.sh
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

: "${ENV_FILE}"
[ -f "$ENV_FILE" ] || die "找不到密钥文件 $ENV_FILE。请先：cp deploy/.env.preview.example deploy/.env.preview 并填入真实机密。"

get() { env_get "$@"; }

# 用于读 src/config.py 配置版本/驱动哈希校验的 Python（优先仓库 venv，否则 python3/python）。
PY_BIN="$(command -v python3 || command -v python || true)"
[ -n "$PY_BIN" ] || warn "未找到 python3/python：弱哈希/明文凭据无法调用脚本校验；DEPLOY_CONFIG_VERSION 回落到 src/config.py 文本默认。"

# 必需的机密/配置（非空、非占位符）。逐项缺失即 fail-closed，拒绝部署。
REQUIRED=(
  POSTGRES_PASSWORD         # PostgreSQL owner/迁移角色口令
  REDIS_PASSWORD            # Redis requirepass
  APP_RUNTIME_PASSWORD      # 运行角色 app_runtime 口令
  AUTH_JWT_SECRET           # JWT HS256 密钥（受限环境缺失 api 启动即 fail-closed）
  AUTH_LOGIN_CREDENTIALS    # 登录凭据表 JSON（未配置则 /api/auth/login fail-closed）
  LLM_API_KEY               # 自托管 OpenAI 兼容端点密钥
  LLM_ALLOWED_HOSTS         # 端点网络白名单（受限环境空即 fail-closed，阻断未批准外联）
  HIGH_CONFIDENCE_MODELS    # 写操作能力矩阵白名单（缺失 → 无模型可写，写全转人工）
  LLM_EVAL_REPORT_PATH      # 写操作评测报告路径（缺失 → 白名单不生效）
  LAUNCH_ALLOWED_TENANTS    # 首批上线租户白名单（verify_launch_gate --strict 要求非空）
  EXECUTION_CALLBACK_HMAC_SECRET  # 回调 HMAC-SHA256 验签密钥
)

missing=0
for k in "${REQUIRED[@]}"; do
  v="$(get "$k")"
  if [ -z "$v" ]; then
    warn "缺少必需项 $k（为空）。"
    missing=1
  elif is_placeholder "$v"; then
    warn "必需项 $k 仍是占位符（$v），须替换为真实注入值。"
    missing=1
  fi
done

# 禁止使用内建默认回调 HMAC 密钥（否则外部任意回均可伪造签名，重复回调/重放防护形同虚设）。
CB="$(get EXECUTION_CALLBACK_HMAC_SECRET || true)"
if [ -n "$CB" ] && [ "$CB" = "shadow-callback-secret" ]; then
  warn "EXECUTION_CALLBACK_HMAC_SECRET 命中内建默认值（shadow-callback-secret）：禁止上线，请注入随机强密钥。"
  missing=1
fi

# 写操作评测报告：路径非空/非占位，且宿主挂载源文件必须存在（docker-compose.preview.yml 以只读挂载它）。
EVAL_PATH="$(get LLM_EVAL_REPORT_PATH || true)"
if [ -z "$EVAL_PATH" ] || is_placeholder "$EVAL_PATH"; then
  warn "LLM_EVAL_REPORT_PATH 缺失/占位 → 能力矩阵无报告可验（写操作 fail-closed 转人工）。"
  missing=1
fi
HOST_EVAL="$REPO_ROOT/evidence/llm_candidate_eval.json"
if [ -f "$HOST_EVAL" ]; then
  log "写操作评测报告存在：$HOST_EVAL"
else
  warn "未找到写操作评测报告 $HOST_EVAL（api 容器将挂载此只读文件；缺失则无模型可写）。"
  missing=1
fi

# 受限环境（preview/production）强制 LAUNCH_GATE_STRICT=true。
ENV_VAL="$(get ENV || true)"
case "$ENV_VAL" in
  preview|production)
    GS="$(get LAUNCH_GATE_STRICT || true)"
    if [ "$GS" != "true" ]; then
      warn "受限环境（ENV=$ENV_VAL）必须 LAUNCH_GATE_STRICT=true；当前=${GS:-<空>}。"
      missing=1
    fi
    ;;
  *)
    warn "非受限环境（ENV=${ENV_VAL:-<空>}）：仍校验机密，但本清单面向 preview/production。"
    ;;
esac

# 执行模式 / 提供方合法性（与 verify_launch_gate 门控一致）。
EXEC_MODE="$(get EXECUTION_MODE || true)"
EXEC_PROVIDER="$(get EXECUTION_PROVIDER || true)"
case "$EXEC_MODE" in
  shadow) : ;;
  live)
    if [ -z "$EXEC_PROVIDER" ] || [ "$EXEC_PROVIDER" = "mock" ]; then
      warn "EXECUTION_MODE=live 且 EXECUTION_PROVIDER=${EXEC_PROVIDER:-<空>}：首次上线只允许 shadow 沙箱，禁止真实资金/业务网关。"
      missing=1
    fi
    ;;
  *) warn "EXECUTION_MODE=${EXEC_MODE:-<空>} 非法（应为 shadow 或 live）。"; missing=1 ;;
esac

# AUTH_JWT_SECRET 为空时 preview 的 api 启动即 fail-closed（见 src/main.py fail_closed_auth_guard）。
if [ "$(get AUTH_JWT_SECRET || true)" = "<inject>" ] || [ -z "$(get AUTH_JWT_SECRET || true)" ]; then
  warn "AUTH_JWT_SECRET 缺失/占位 → 受限环境 api 启动将 fail-closed（有意为之）。"
fi

# 发布记录必填：DEPLOY_CONFIG_VERSION 非空、非占位（配置漂移审计）。未注入时回落到 src/config.py 默认。
DCV="$(get DEPLOY_CONFIG_VERSION || true)"
if [ -z "$DCV" ]; then DCV="$(config_version_default)"; fi
if [ -z "$DCV" ]; then
  warn "DEPLOY_CONFIG_VERSION 缺失（既未在 ENV_FILE 注入，src/config.py 也无默认）。请在 ENV_FILE 注入（如 0.1.0）。"
  missing=1
elif is_placeholder "$DCV"; then
  warn "DEPLOY_CONFIG_VERSION 仍为占位符（$DCV）。"
  missing=1
else
  log "DEPLOY_CONFIG_VERSION=$DCV（用于发布记录配置版本）。"
fi

# 登录凭据哈希算法：仅允许 argon2id|bcrypt（显式禁止 sha256/md5 等弱哈希）。未设则默认 argon2id（安全）。
ACH="$(get AUTH_CREDENTIAL_HASH || true)"
if [ -z "$ACH" ]; then
  ACH="argon2id"; log "AUTH_CREDENTIAL_HASH 未显式设置，采用默认 argon2id。"
elif [ "$ACH" != "argon2id" ] && [ "$ACH" != "bcrypt" ]; then
  warn "AUTH_CREDENTIAL_HASH=${ACH} 非法（仅允许 argon2id|bcrypt；禁止 sha256/md5 等弱哈希）。"
  missing=1
else
  log "AUTH_CREDENTIAL_HASH=${ACH}（合法）。"
fi

# DEMO_SEED_ENABLED：preview/production 必须 false（禁用 seed_default 预置演示租户）。
SEED_VAL="$(get DEMO_SEED_ENABLED || true)"
case "$SEED_VAL" in
  true|TRUE|True|1|yes|YES|on|ON)
    case "$(get ENV || true)" in
      preview|production)
        warn "受限环境（ENV=$ENV_VAL）禁止 DEMO_SEED_ENABLED=${SEED_VAL}：演示/种子数据一律禁用，真实租户走受控迁移/运维脚本。"
        missing=1 ;;
      *) log "DEMO_SEED_ENABLED=${SEED_VAL} 仅限本地开发/test（非 preview/production）。" ;;
    esac ;;
  ''|false|FALSE|False|0|no|NO)
    : ;;  # 禁用（安全默认）
  *) warn "DEMO_SEED_ENABLED=${SEED_VAL} 非法（应为 true/false）。"; missing=1 ;;
esac

# LOGIN_RATE_LIMIT_STORE=redis 时要求 REDIS_URL 已配置（否则分布式限流后端不可用）。
RLS="$(get LOGIN_RATE_LIMIT_STORE || true)"
if [ "$RLS" = "redis" ]; then
  if [ -z "$(get REDIS_URL || true)" ]; then
    warn "LOGIN_RATE_LIMIT_STORE=redis 但 REDIS_URL 未配置（登录分布式限流后端不可用）。"
    missing=1
  else
    log "LOGIN_RATE_LIMIT_STORE=redis 且 REDIS_URL 已配置。"
  fi
fi

# 登录凭据：禁止明文 / 无盐 SHA-256（弱哈希）。优先调 security-eng 的 hash_login_credentials.py --check-env；
# 脚本缺 --check-env（旧/并存版本）或不存在时，用本地指纹回退：sha256-hex / AUTH_CREDENTIAL_HASH 算法合法性。
CREDS="$(get AUTH_LOGIN_CREDENTIALS || true)"
CRED_HASH_ALGO="$(get AUTH_CREDENTIAL_HASH || true)"

fingerprint_cred_gate() {
  if echo "$CREDS" | grep -qE '[0-9a-f]{64}'; then
    warn "AUTH_LOGIN_CREDENTIALS 含 64 位十六进制（sha256 弱哈希）值：禁止无盐弱哈希。"
    missing=1
  elif [ -n "$CRED_HASH_ALGO" ] && [ "$CRED_HASH_ALGO" != "argon2id" ] && [ "$CRED_HASH_ALGO" != "bcrypt" ]; then
    warn "AUTH_CREDENTIAL_HASH=${CRED_HASH_ALGO} 非法（仅 argon2id|bcrypt）。"
    missing=1
  else
    log "登录凭据未命中 sha256 弱哈希；AUTH_CREDENTIAL_HASH=${CRED_HASH_ALGO:-argon2id（默认）}。"
  fi
  return 0
}

if [ -n "$CREDS" ] && [ "$CREDS" != "<inject-json>" ]; then
  HASH_SCRIPT="$REPO_ROOT/scripts/hash_login_credentials.py"
  if [ -n "$PY_BIN" ] && [ -f "$HASH_SCRIPT" ] \
     && "$PY_BIN" "$HASH_SCRIPT" --help 2>/dev/null | grep -q -- '--check-env'; then
    if ! "$PY_BIN" "$HASH_SCRIPT" --check-env; then
      warn "hash_login_credentials.py --check-env 未通过：存在弱哈希/明文/算法不符的登录凭据。"
      missing=1
    else
      log "hash_login_credentials.py --check-env 通过（argon2id/bcrypt PHC）。"
    fi
  else
    fingerprint_cred_gate
  fi
fi

# 明文凭据文件：仅本地生成用（gitignored）；若为模板则只校验格式，否则不允许含明文（fail-closed）。
CRED_TXT="$REPO_ROOT/deploy/secrets/preview_login_credentials.txt"
if [ -f "$CRED_TXT" ]; then
  if grep -qiE 'template|模板|placeholder|<inject>' "$CRED_TXT"; then
    log "明文凭据文件已是模板（仅校验格式）：$CRED_TXT"
  elif grep -qE '=[[:space:]]*[A-Za-z0-9_!@#~-]+$' "$CRED_TXT"; then
    warn "明文凭据文件含明文登录凭据：${CRED_TXT}（请改用 argon2id/bcrypt 哈希，或转为模板）。"
    missing=1
  fi
fi

if [ "$missing" -ne 0 ]; then
  die "存在缺失/占位/被禁的机密或门控未通过，拒绝部署（fail-closed）。"
fi

log "机密检查通过：$(IFS=,; echo "${REQUIRED[*]}") 均已注入（回调 HMAC 为非默认值，LAUNCH_GATE_STRICT=true）。"
