#!/usr/bin/env bash
# gen_certs.sh —— TLS 证书生成 / 导入 / 受信机制（preview 自签 + production 受信 CA 落地）。
#
# 作用：为反向代理（nginx）准备 /etc/nginx/certs（即仓库 deploy/secrets/certs）目录下的证书。
#  - selfsigned [domain]：生成自签名证书，仅用于 preview / 内部预发布验证（**不得**用于正式生产）。
#  - import <fullchain> <privkey> [--domain D]：把【受信 CA / 内部 CA / Let's Encrypt】签发的
#    全链(leaf+intermediate)与私钥落地为 deploy/secrets/certs/server.{crt,key}，并校验链与 SAN。
#  - letsencrypt <domain> [--email E]：用 certbot（宿主机或 docker）向 Let's Encrypt 签发并落地；
#    需要**外部可公网解析的域名** + 通往外部的 ACME 通道（HTTP-01 / DNS-01），属边界4 外部输入。
#  - status：显示当前证书主体 / 签发者 / 有效期 / SAN / 是否自签。
#
# 生产红线：正式生产必须使用**受信 CA 证书链**（非自签）。当前仓库默认自签（CN=preview.local）→ BLOCKED，
# 须由业务方提供【正式域名 + 受信 CA 供应商】，见 deploy/TLS_TRUSTED_CERTS.md 与团队工作区 TLS 报告。
#
# 用法：bash deploy/scripts/gen_certs.sh <mode> [args...]
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

CERT_DIR="$DEPLOY_DIR/secrets/certs"
mkdir -p "$CERT_DIR"
CRT="$CERT_DIR/server.crt"
KEY="$CERT_DIR/server.key"

need_openssl() {
  if ! command -v openssl >/dev/null 2>&1; then
    die "未找到 openssl。请安装 OpenSSL（apt/yum/apk 或 Windows/OpenSSL），证书操作必需。"
  fi
}

usage() {
  cat >&2 <<'EOF'
gen_certs.sh <mode> [args...]
  selfsigned [domain]               生成自签名证书（默认；仅 preview/dev）
  import    <fullchain> <privkey> [--domain D]   导入受信 CA 全链+私钥 → deploy/secrets/certs
  letsencrypt <domain> --email E [--webroot DIR] 用 certbot 签发并落地（需外部域名+ACME 通道）
  status                              显示当前证书信息与是否自签
EOF
  exit 1
}

# ---- 工具：从证书文件里抽 SAN 里的 DNS 名 ----
# 输出形如 `DNS:example.com, DNS:www.example.com` 中的 example.com、www.example.com（每行一个）。
cert_sans() {
  local f="$1"
  # -ext subjectAltName 输出：`DNS:example.com, IP Address:127.0.0.1, DNS:www...`
  openssl x509 -in "$f" -noout -ext subjectAltName 2>/dev/null \
    | tr ',' '\n' \
    | sed -n 's/^[[:space:]]*DNS://p' \
    | tr -d '\r' \
    | sort -u
}

# 是否自签：leaf 的 Subject == Issuer。
is_selfsigned() {
  local f="$1"
  local sub iss
  sub="$(openssl x509 -in "$f" -noout -subject -nameopt multiline 2>/dev/null | sed -n 's/^[[:space:]]*subject=//p')"
  iss="$(openssl x509 -in "$f" -noout -issuer -nameopt multiline 2>/dev/null | sed -n 's/^[[:space:]]*issuer=//p')"
  # 简化：直接比较单行 subject / issuer。
  sub="$(openssl x509 -in "$f" -noout -subject 2>/dev/null)"
  iss="$(openssl x509 -in "$f" -noout -issuer 2>/dev/null)"
  [ "$sub" = "$iss" ]
}

# 校验某 fullchain 文件的链/期/SAN，供 import 前后自检。
validate_fullchain() {
  local file="$1" domain="${2:-}"
  need_openssl
  [ -f "$file" ] || die "未找到证书文件：$file"
  # leaf 必须能解析
  openssl x509 -in "$file" -noout -subject >/dev/null 2>&1 \
    || die "证书文件无效（openssl 无法解析 leaf）：$file"
  # 有效期
  local notafter
  notafter="$(openssl x509 -in "$file" -noout -enddate 2>/dev/null | sed 's/notAfter=//')"
  [ -n "$notafter" ] || die "无法读取证书有效期：$file"
  log "证书有效期至：${notafter}"
  # 全链证书数（含 leaf + 中间链）。受信应 >=2（leaf+at least one intermediate，根通常不在链中）。
  local n
  n="$(openssl crl2pkcs7 -nocrl -certfile "$file" 2>/dev/null | openssl pkcs7 -print_certs -noout 2>/dev/null | grep -c '^subject=' || true)"
  [ "${n:-1}" -ge 1 ] || n=1
  log "全链含 ${n} 张证书（leaf + intermediates）。"
  # 自签检测
  if is_selfsigned "$file"; then
    warn "当前证书为【自签名】（Subject==Issuer）——仅可用于 preview/内部验证，正式生产必须替换为受信 CA。"
  fi
  # SAN 校验（受信证书必须含目标域名）
  if [ -n "$domain" ]; then
    if cert_sans "$file" | grep -qx "$domain"; then
      log "SAN 包含目标域名：$domain"
    else
      warn "SAN 未包含目标域名：$domain（现有 SAN：$(cert_sans "$file" | paste -sd, -)）。请确认域名/证书匹配。"
    fi
  fi
}

# 校验私钥与证书是否配对（模数一致）。
key_matches_cert() {
  local cert="$1" key="$2"
  local m1 m2
  m1="$(openssl x509 -in "$cert" -noout -modulus 2>/dev/null | openssl sha256 | awk '{print $2}')"
  m2="$(openssl rsa -in "$key" -noout -modulus 2>/dev/null | openssl sha256 | awk '{print $2}')"
  [ -n "$m1" ] && [ "$m1" = "$m2" ]
}

# ===================== 模式：selfsigned =====================
cmd_selfsigned() {
  local domain="${1:-preview.local}"
  need_openssl
  if [ -f "$CRT" ] && [ -f "$KEY" ]; then
    if is_selfsigned "$CRT"; then
      warn "已存在自签名证书：$CRT / $KEY（跳过生成；如需更换请先删除或执行 import）。"
      return 0
    else
      warn "检测到 $CRT 为【受信/非自签】证书，跳过自签名生成（避免覆盖受信证书）。如需覆盖请手动删除。"
      return 0
    fi
  fi
  log "为域 $domain 生成自签名证书（有效期 365 天）..."
  openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
    -keyout "$KEY" -out "$CRT" \
    -subj "/CN=$domain" \
    -addext "subjectAltName=DNS:$domain,DNS:localhost,IP:127.0.0.1" >/dev/null 2>&1
  chmod 600 "$KEY"
  log "自签名证书已生成：$CRT / $KEY（CN=$domain）"
  warn "自签名证书浏览器会告警；正式生产必须替换为受信 CA 证书（见 import / letsencrypt / TLS_TRUSTED_CERTS.md）。"
}

# ===================== 模式：import（受信 CA / 内部 CA / Let's Encrypt） =====================
cmd_import() {
  local fullchain="${1:-}" privkey="" domain=""
  [ -n "$fullchain" ] || { usage; }
  shift 1
  # 解析剩余：可选 [privkey] 与 [--domain D]
  while [ $# -gt 0 ]; do
    case "$1" in
      --domain) domain="${2:-}"; shift 2 ;;
      *) if [ -z "$privkey" ]; then privkey="$1"; shift 1; else domain="$1"; shift 1; fi ;;
    esac
  done
  need_openssl
  [ -f "$fullchain" ] || die "fullchain 不存在：$fullchain"
  [ -n "$privkey" ] || privkey="$(dirname "$fullchain")/privkey.pem"
  [ -f "$privkey" ] || die "privkey 不存在：$privkey（请传全链+私钥两个参数）"

  log "导入前自检 fullchain：$fullchain"
  validate_fullchain "$fullchain" "$domain"

  # 私钥与证书配对校验
  if key_matches_cert "$fullchain" "$privkey"; then
    log "私钥与证书配对（模数一致）。"
  else
    die "私钥与证书不匹配（模数不同）：请核对 privkey / fullchain 是否来自同一签发。"
  fi

  # 落地：全链写 server.crt（含中间链，nginx 直接使用），私钥写 server.key；覆盖前备份。
  if [ -f "$CRT" ]; then
    mv -f "$CRT" "$CRT.bak.$(date +%Y%m%d%H%M%S)" 2>/dev/null || true
    log "已备份原证书为 $CRT.bak.*"
  fi
  if [ -f "$KEY" ]; then
    mv -f "$KEY" "$KEY.bak.$(date +%Y%m%d%H%M%S)" 2>/dev/null || true
  fi
  cp -f "$fullchain" "$CRT"
  cp -f "$privkey" "$KEY"
  chmod 600 "$KEY"
  chmod 644 "$CRT"

  log "受信证书已落地："
  log "  CRT（全链）= $CRT"
  log "  KEY        = $KEY"
  log "落地后请执行：bash deploy/scripts/verify_tls.sh --host 127.0.0.1 --https-port 8843 --http-port 8080 --expect-domain <正式域名>"
  if [ -n "$domain" ]; then
    log "请同步把 nginx prod.conf 的 server_name 设为：${domain}"
  fi
}

# ===================== 模式：letsencrypt（需外部域名 + ACME 通道） =====================
cmd_letsencrypt() {
  local domain="${1:-}" email="" webroot=""
  shift 1 || true
  while [ $# -gt 0 ]; do
    case "$1" in
      --email) email="${2:-}"; shift 2 ;;
      --webroot) webroot="${2:-}"; shift 2 ;;
      *) echo "未知参数：$1" >&2; usage ;;
    esac
  done
  [ -n "$domain" ] || { usage; }
  need_openssl

  # 生产红线：正式域名 + 外部 ACME 通道为【边界4 外部输入】。此处仅实现机制，不代签。
  warn "Let's Encrypt 签发需要：① 已解析到本机的正式域名；② 通往外部的 ACME 通道（HTTP-01 需 80/443 可达，DNS-01 需 DNS 控制）。这两项均为外部输入，缺一不可。"
  [ -n "$email" ] || die "请提供 --email（ACME 注册/通知邮箱），Let's Encrypt 必填。"

  if command -v certbot >/dev/null 2>&1; then
    log "使用宿主机 certbot 签发（standalone/webroot）..."
    local args=(certonly --non-interactive --agree-tos --email "$email" --domains "$domain")
    if [ -n "$webroot" ]; then
      args+=(--webroot -w "$webroot")
    else
      args+=(--standalone)
    fi
    if certbot "${args[@]}"; then
      local live="/etc/letsencrypt/live/$domain"
      [ -f "$live/fullchain.pem" ] && [ -f "$live/privkey.pem" ] || die "certbot 未产出预期文件：$live"
      log "certbot 签发成功，导入到 deploy/secrets/certs ..."
      cmd_import "$live/fullchain.pem" "$live/privkey.pem" --domain "$domain"
    else
      die "certbot 签发失败。请核对域名解析/ACME 通道/端口可达，重试。"
    fi
  else
    # 未有宿主机 certbot → 用 docker 容器（自托管，符合完全自托管红线：仅经 certbot 官方镜像外联 ACME）。
    if command -v "$DOCKER" >/dev/null 2>&1 && "$DOCKER" info >/dev/null 2>&1; then
      log "未找到宿主机 certbot，使用 docker certbot/certbot 签发（webroot 模式，挂载 /etc/letsencrypt）..."
      local webroot_mnt="${webroot:-/var/www/certbot}"
      "$DOCKER" run --rm \
        -v "$DEPLOY_DIR/letsencrypt:/etc/letsencrypt" \
        -v "$webroot_mnt:/webroot" \
        -p 80:80 \
        certbot/certbot certonly --webroot -w /webroot \
        --non-interactive --agree-tos --email "$email" --domains "$domain" \
        || die "certbot(docker) 签发失败。"
      local live="$DEPLOY_DIR/letsencrypt/live/$domain"
      [ -f "$live/fullchain.pem" ] && [ -f "$live/privkey.pem" ] || die "certbot(docker) 未产出文件：$live"
      cmd_import "$live/fullchain.pem" "$live/privkey.pem" --domain "$domain"
    else
      die "未找到 certbot（宿主机或 Docker 容器）且 Docker 不可用。受信证书需外部提供，见 TLS_TRUSTED_CERTS.md。"
    fi
  fi
}

# ===================== 模式：status =====================
cmd_status() {
  need_openssl
  if [ ! -f "$CRT" ]; then
    echo "无证书：$CRT 不存在。先执行 gen_certs.sh selfsigned 或 import/letsencrypt。"
    return 0
  fi
  echo "证书文件：$CRT"
  echo "  主体  : $(openssl x509 -in "$CRT" -noout -subject 2>/dev/null || echo '<解析失败>')"
  echo "  签发者: $(openssl x509 -in "$CRT" -noout -issuer 2>/dev/null || echo '<解析失败>')"
  echo "  有效期: $(openssl x509 -in "$CRT" -noout -dates 2>/dev/null | tr '\n' ' ')"
  echo "  SAN   : $(cert_sans "$CRT" | paste -sd, -)"
  if is_selfsigned "$CRT"; then
    warn "现状=自签名（Subject==Issuer）→ 正式生产【不可用】，需受信 CA 替换（BLOCKED_EXTERNAL）。"
  else
    ok_marker="受信/非自签"
    echo "  类型  : ${ok_marker}（请再以 verify_tls.sh / 链验证确认受信链完整）"
  fi
}

# ===================== 入口 =====================
MODE="${1:-selfsigned}"
case "$MODE" in
  selfsigned) shift 1 || true; cmd_selfsigned "$@" ;;
  import)     shift 1 || true; cmd_import "$@" ;;
  letsencrypt) shift 1 || true; cmd_letsencrypt "$@" ;;
  status)     shift 1 || true; cmd_status ;;
  *) usage ;;
esac
