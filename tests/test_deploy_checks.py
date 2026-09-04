"""部署前检查 / 部署管线脚本级与逻辑级测试。

背景（t4）：`deploy/scripts/` 由 platform-eng 负责，本文件**只读**脚本并验证其具备的安全
能力，不修改任何 `deploy/scripts/*`。本机无 Docker、WSL bash 与 git 在 Windows path 的
可执行性问题，无法实测 `docker build` 的"同 tag 重建同一 digest"，因此采用**脚本级/逻辑级**
覆盖（读取脚本源码断言关键门禁逻辑 + 用 git 命令实测 ref/dirty 判定），并在报告中说明
"同 tag 重建同 digest"为**未实测**（无 Docker），给出可复核证据。

覆盖验收项：
- 脏工作区被拒（repo_is_dirty + fail-closed 拒绝）。
- 干净 ref 从【干净 worktree】构建（git worktree add → compose build --pull --provenance=false --sbom=false）。
- 发布记录含 commit / digest / 构建时间 / 配置版本 / 可复现性字段。
- check_secrets 门控：受限环境禁 DEMO_SEED_ENABLED、DEPLOY_CONFIG_VERSION 非空/非占位、
  AUTH_CREDENTIAL_HASH 仅 argon2id|bcrypt、弱哈希检测。
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from src.config import Settings

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "deploy" / "scripts"


def _script(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8")


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(REPO), *args],
                          capture_output=True, text=True, timeout=30)


_HAS_GIT = shutil.which("git") is not None


# ---------------------------------------------------------------------------
# 1. 脚本存在且非空
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["common.sh", "deploy.sh", "pre_deploy_checks.sh",
                                  "check_secrets.sh", "verify_fail_closed.sh"])
def test_deploy_scripts_exist_and_nonempty(name):
    p = SCRIPTS / name
    assert p.exists(), f"缺失部署脚本：{name}"
    assert p.stat().st_size > 200, f"部署脚本过小/疑似为空：{name}"


def test_deploy_scripts_are_shell_with_shebangs():
    for name in ("deploy.sh", "pre_deploy_checks.sh", "check_secrets.sh", "common.sh"):
        assert _script(name).startswith("#!/usr/bin/env bash"), f"{name} 缺少 bash shebang"


# ---------------------------------------------------------------------------
# 2. 脏工作区被拒（repo_is_dirty + fail-closed）
# ---------------------------------------------------------------------------
def test_pre_deploy_checks_enforces_clean_worktree():
    text = _script("pre_deploy_checks.sh")
    # 干净度门禁：必须调用 repo_is_dirty / git status --porcelain，且脏即 fail=1。
    assert "repo_is_dirty" in text
    assert "git status --porcelain" in text
    assert "--allow-dirty" in text  # 显式放行开关（该分支明确警告不推荐）
    assert "fail=1" in text         # 脏 → 记失败
    assert "die" in text            # 任何失败 → 非零退出 fail-closed


def test_common_sh_repo_is_dirty_uses_porcelain():
    text = _script("common.sh")
    m = re.search(r"repo_is_dirty\(\) \{([^}]*)\}", text)
    assert m, "common.sh 缺少 repo_is_dirty 函数"
    body = m.group(1)
    assert "git" in body and "status --porcelain" in body


def test_deploy_script_rejects_dirty_worktree_without_allow_dirty():
    text = _script("deploy.sh")
    assert "repo_is_dirty" in text
    # 无 --allow-dirty 时 `die`（拒绝构建），带 --allow-dirty 才放行（并明确警告）。
    assert "--allow-dirty" in text
    assert "拒绝构建" in text


# ---------------------------------------------------------------------------
# 3. 干净 ref 从干净 worktree 构建（可复现镜像）
# ---------------------------------------------------------------------------
def test_deploy_builds_from_clean_worktree():
    common = _script("common.sh")
    # 干净 worktree：git worktree add --detach <ref>，避免主工作树未提交/未跟踪文件进入。
    assert "git worktree add" in common
    assert "build_from_worktree" in common
    assert "--detach" in common
    # 可复现性：关闭 BuildKit provenance/attestation 清单，digest 字节级可复现。
    assert "--provenance=false --sbom=false" in common
    assert "compose build --pull" in common
    # 用 ref 的干净 worktree 作为 compose 文件与 build context。
    assert "docker-compose.preview.yml" in common


def test_deploy_script_calls_build_from_worktree_with_clean_semantics():
    text = _script("deploy.sh")
    assert "build_from_worktree" in text
    assert "git_ref_commit" in text


# ---------------------------------------------------------------------------
# 4. 发布记录含 commit / digest / 构建时间 / 配置版本 / 可复现性
# ---------------------------------------------------------------------------
def test_publish_record_contains_required_fields():
    common = _script("common.sh")
    # 必须包含这些字段（验收项：发布记录含 commit/digest/构建时间/配置版本）。
    for token in ("git commit", "镜像 digest", "构建时间", "配置版本", "DEPLOY_CONFIG_VERSION",
                  "可复现性", "干净 worktree"):
        assert token in common, f"发布记录缺少关键字段：{token}"


def test_publish_record_writes_markdown_to_record_dir_in_deploy():
    deploy = _script("deploy.sh")
    assert "RECORD_DIR" in deploy and "records" in deploy
    assert "write_publish_record" in deploy
    # 发布记录命名含时间戳（可追溯）。
    assert "PRODUCTION_DEPLOYMENT_record" in deploy
    # deploy 同时把 ref/commit/config_version/build_ts/digests 追加到 rollback.log。
    assert "rollback.log" in deploy


# ---------------------------------------------------------------------------
# 5. check_secrets 门控
# ---------------------------------------------------------------------------
def test_check_secrets_gates_demo_seed_and_config_version():
    text = _script("check_secrets.sh")
    assert "DEMO_SEED_ENABLED" in text
    assert "DEPLOY_CONFIG_VERSION" in text
    # 受限环境必须 LAUNCH_GATE_STRICT=true。
    assert "LAUNCH_GATE_STRICT" in text
    # 登录凭据哈希仅允许 argon2id|bcrypt。
    assert "argon2id" in text and "bcrypt" in text
    assert "AUTH_CREDENTIAL_HASH" in text


def test_check_secrets_detects_weak_credentials():
    text = _script("check_secrets.sh")
    # 弱哈希（64 位十六进制 = 无盐 SHA-256 / 32 位 = MD5）检测。
    assert "[0-9a-f]{64}" in text
    assert "弱哈希" in text


def test_check_secrets_requires_callback_hmac_not_default():
    text = _script("check_secrets.sh")
    assert "shadow-callback-secret" in text
    assert "EXECUTION_CALLBACK_HMAC_SECRET" in text


# ---------------------------------------------------------------------------
# 6. git 命令实测（Windows 侧 git）：ref 解析 / 脏判定
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not _HAS_GIT, reason="git 不在 PATH，跳过 git 实测")
def test_git_ref_resolves_to_commit():
    r = _git("rev-parse", "--verify", "HEAD^{commit}")
    assert r.returncode == 0, r.stderr
    assert len(r.stdout.strip()) == 40  # 完整 SHA-1


@pytest.mark.skipif(not _HAS_GIT, reason="git 不在 PATH，跳过 git 实测")
def test_git_status_porcelain_command_available():
    # git_ref_commit / repo_is_dirty 依赖的 `git status --porcelain` 命令路径可运行。
    r = _git("status", "--porcelain")
    assert r.returncode == 0, r.stderr
    assert r.stdout is not None


@pytest.mark.skipif(not _HAS_GIT, reason="git 不在 PATH，跳过 git 实测")
def test_common_sh_git_ref_commit_command_matches():
    # common.sh 用 `git rev-parse --verify <ref>^{commit}` 解析引用 → 与实测命令一致。
    common = _script("common.sh")
    assert "rev-parse --verify" in common and "^{commit}" in common
    r = _git("rev-parse", "--verify", "HEAD^{commit}")
    assert r.returncode == 0


# ---------------------------------------------------------------------------
# 7. 配置版本解析（发布记录配置版本来源）
# ---------------------------------------------------------------------------
def test_config_version_default_parse():
    config_text = (REPO / "src" / "config.py").read_text(encoding="utf-8")
    m = re.search(r'deploy_config_version\s*:\s*str\s*=\s*"([^"]+)"', config_text)
    assert m, "config.py 缺少 deploy_config_version 默认"
    assert m.group(1).strip()
    # 与运行时 Settings 默认一致。
    assert m.group(1) == Settings().deploy_config_version
