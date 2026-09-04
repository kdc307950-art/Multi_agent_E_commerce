#!/usr/bin/env bash
# common.sh —— preview 部署公共助手（由其它脚本 source）。
# 固定 Compose 文件与密钥文件路径，并提供幂等的 compose 调用封装与配色日志。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(dirname "$SCRIPT_DIR")"                       # deploy/
REPO_ROOT="$(dirname "$DEPLOY_DIR")"                       # 仓库根

# compose 文件位于仓库根（与既有 docker-compose.yml 同级），相对路径（./deploy/...）以 compose 文件目录解析。
COMPOSE_FILE="${COMPOSE_FILE:-$REPO_ROOT/docker-compose.preview.yml}"
ENV_FILE="${ENV_FILE:-$DEPLOY_DIR/.env.preview}"

# @deploy 入口：docker compose --env-file <ENV_FILE> -f <COMPOSE_FILE> <args...>
compose() {
  "$DOCKER" compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

log()  { printf '\n\033[1;34m[%s]\033[0m %s\n' "$(date '+%H:%M:%S')" "$*"; }
warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }

# 允许覆盖 docker 二进制（测试/CI）。
DOCKER="${DOCKER:-docker}"
maybe_docker() {
  command -v "$DOCKER" >/dev/null 2>&1 || die "未找到 Docker（$DOCKER）。请先安装并启动 Docker 引擎。"
}

# 从 ENV_FILE 读取指定键的值（兼容 KEY=VALUE 与注释；去掉首尾引号与 CR（CRLF 换行））。找不到返回空串。
env_get() {
  grep -E "^$1=" "$ENV_FILE" | head -n1 | cut -d= -f2- | tr -d '"' | tr -d "'" | tr -d '\r' || true
}

# 判断值是否为占位符（含 < 或 >，如 <inject>）。
is_placeholder() { case "$1" in *'<'*|*'>'*) return 0;; *) return 1;; esac; }

# ---- 部署基线：git 干净度 / worktree 干净构建（发布可复现镜像）----

# 仓库工作区是否脏：`git status --porcelain` 非空即脏（含未提交/未跟踪）。
# 返回 0=脏，1=干净。只读判定，不做任何改动。
repo_is_dirty() {
  [ -n "$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null)" ]
}

# 解析任意 git 引用（tag / commit / 分支 / HEAD）为完整 commit 哈希；不存在返回空串。
# 用 rawin 的 `rev-parse --verify <ref>^{commit}`，避免把歧义的物件名当 commit。
git_ref_commit() {
  git -C "$REPO_ROOT" rev-parse --verify "$1^{commit}" 2>/dev/null || true
}

# 读 src/config.py 的 deploy_config_version 默认（发布记录配置版本；不依赖 pydantic 导入）。
# 匹配形如 `deploy_config_version: str = "0.1.0"` 的默认值并取引号内版本号。
config_version_default() {
  if [ -f "$REPO_ROOT/src/config.py" ]; then
    grep -Eo 'deploy_config_version[[:space:]]*:[[:space:]]*str[[:space:]]*=[[:space:]]*"[^"]+"' "$REPO_ROOT/src/config.py" \
      | grep -Eo '"[^"]+"' | head -n1 | tr -d '"' || true
  fi
}

# 从指定 git 引用检出【干净 worktree】到临时目录，并以该 worktree 为 compose 文件与 build context
# 执行 `compose build`。worktree 只包含该引用下受版本控制的文件（主工作树未跟踪/未提交文件不进入），
# 从而保证镜像只用「此引用下已提交的源码」构建 → 干净 tag/commit 可独立复现。
# 构建后收集各服务镜像 digest（内容 Id，可复现性指纹）到全局数组 BUILD_DIGESTS，并清理 worktree。
# 用法：build_from_worktree <ref> [project-name]
#   project-name 缺省为 compose 项目名 `after-sales-preview`（与 docker-compose.preview.yml 的 name: 一致）。
BUILD_DIGESTS=()
BUILD_PROJECT="${BUILD_PROJECT:-after-sales-preview}"
build_from_worktree() {
  local ref="$1" proj="${2:-$BUILD_PROJECT}"
  local full wt compose_file rc
  full="$(git_ref_commit "$ref")"
  if [ -z "$full" ]; then die "git 引用不存在：$ref（无法解析为 commit）。"; fi

  wt="$(mktemp -d)" || die "无法创建临时目录用于干净 worktree。"
  # git worktree add 允许目标目录为空或不存在；mktemp 建了空目录，直接复用。
  if ! git -C "$REPO_ROOT" worktree add --detach "$wt" "$full" >/dev/null 2>&1; then
    rm -rf "$wt" 2>/dev/null || true
    git -C "$REPO_ROOT" worktree prune >/dev/null 2>&1 || true
    die "无法从引用 ${ref}（${full}）创建干净 worktree（是否无提交权限/引用不可用？）。"
  fi

  # 任何退出都清理 worktree，避免残留。
  cleanup_wt() {
    git -C "$REPO_ROOT" worktree remove --force "$wt" >/dev/null 2>&1 \
      || git -C "$REPO_ROOT" worktree prune >/dev/null 2>&1 || true
    rm -rf "$wt" 2>/dev/null || true
  }
  # 正常返回时也清理（die 用 exit 不触发 RETURN，需在其前的显式 cleanup_wt 处理）。
  trap cleanup_wt RETURN

  compose_file="$wt/docker-compose.preview.yml"
  if [ ! -f "$compose_file" ]; then
    cleanup_wt; die "worktree 缺少 compose 文件：$compose_file（该引用下无 docker-compose.preview.yml？）。"
  fi

  log "用干净 worktree 构建（引用 $ref / $full）：$wt"
  (
    # 以 worktree 目录为 CWD：compose 相对路径（context:. 、context:./frontend、./deploy/* 挂载）
    # 均以该引用下的文件为基准解析；ENV_FILE/密钥文件仍用真实绝对/仓库相对路径注入。
    # --provenance=false --sbom=false：关闭 BuildKit 的 provenance/attestation 清单，使镜像
    #   manifest-list / repo digest 字节级可复现（否则 attestation 清单含随机 provenance，digest 漂移）。
    cd "$wt" || exit 1
    "$DOCKER" compose --env-file "$ENV_FILE" -f "$compose_file" build --pull --provenance=false --sbom=false
  )
  rc=$?
  if [ "$rc" -ne 0 ]; then
    cleanup_wt; die "worktree 构建失败（exit=$rc）。回滚/复查：docker compose -f \"$compose_file\" build"
  fi

  # 收集镜像 content digest（可复现指纹）；本地构建先落 image id，入 registry 后为 RepoDigest。
  BUILD_DIGESTS=()
  for svc in api migrate worker frontend; do
    local d
    d="$("$DOCKER" image inspect "${proj}-${svc}:latest" --format '{{.Id}}' 2>/dev/null \
        | sed -E 's/^sha256://' || true)"
    BUILD_DIGESTS+=("${svc}=${d}")
  done

  cleanup_wt
  log "干净 worktree 构建完成（引用 $ref / $full）：API/build context 均来自该引用的已提交源码。"
}

# 生成一份发布记录（Markdown）：含 Git commit(full+short)、镜像 digest、构建时间、配置版本、
# worktree/干净说明、可复现性检查结论。供 deploy.sh / rollback.sh 在发布后调用。
# 用法：write_publish_record <outfile> <git-ref>
# 依赖（可缺省，缺省则填入当前可观测默认值）：BUILD_CONFIG_VERSION、BUILD_TIMESTAMP、
#   BUILD_DIGESTS（数组，格式 `svc=digest`）、ALLOW_DIRTY、BUILD_REPRODUCIBLE。
write_publish_record() {
  local out="$1" ref="$2"
  local full short cfg build_ts note has_dirty svc dg
  full="$(git_ref_commit "$ref")"
  if [ -z "$full" ]; then full="<unresolved>"; short="<unresolved>"; else short="${full:0:7}"; fi
  cfg="${BUILD_CONFIG_VERSION:-<unset>}"
  build_ts="${BUILD_TIMESTAMP:-$(date '+%Y-%m-%d %H:%M:%S %z')}"
  has_dirty=""; [ "${ALLOW_DIRTY:-0}" = "1" ] && has_dirty="（本次部署显式 --allow-dirty）"
  note="本次从 git 引用 \`${ref}\`（\`${full}\`）的【干净 worktree】检出构建：build context 仅包含该引用下已提交（受版本控制）的源码；主工作树未提交/未跟踪文件不进入构建。${has_dirty}"
  repro="${BUILD_REPRODUCIBLE:-待实测}"
  { echo "# 发布记录（部署基线自动生成）"
    echo ""
    echo "> 由 \`deploy/scripts/deploy.sh\`（或 \`rollback.sh\`）在发布后生成。仅记录**本次可观测事实**，不臆造。"
    echo ""
    echo "## 记录标识"
    echo "- 记录 ID：\`PROD_DEPLOY-$(date '+%Y%m%d-%H%M%S')\`"
    echo "- 目标环境：\`preview\`（项目 \`after-sales-preview\`）"
    echo "- 生成时间：${build_ts}"
    echo ""
    echo "## Git 引用与构建"
    echo "- git ref：\`${ref}\`"
    echo "- git commit（完整）：\`${full}\`"
    echo "- git commit（短）：\`${short}\`"
    echo "- 构建时间：${build_ts}"
    echo "- 环境配置版本（DEPLOY_CONFIG_VERSION）：\`${cfg}\`"
    echo "- 构建方式：干净 worktree（\`git worktree add\`）→ \`compose build --pull\`"
    echo "- 说明：${note}"
    echo ""
    echo "## 镜像 digest（content Id / repo digest）"
    echo "| 服务 | digest |"
    echo "|------|--------|"
    if [ "${#BUILD_DIGESTS[@]}" -gt 0 ]; then
      for d in "${BUILD_DIGESTS[@]}"; do
        svc="${d%%=*}"; dg="${d#*=}"
        echo "| ${svc} | \`${dg}\` |"
      done
    else
      echo "| （未采集） | （未采集） |"
    fi
    echo ""
    echo "## 可复现性检查"
    echo "- 同 ref 干净重建是否得到同一镜像 digest：**${repro}**"
  } > "$out"
  log "发布记录已写入：$out"
}
