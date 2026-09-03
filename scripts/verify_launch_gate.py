"""verify_launch_gate.py — 首次上线门控核验脚本（可本地/CI 运行）。

覆盖《上线阈值 / 首启约束》：
- 少量租户：受限环境必须配置 LAUNCH_ALLOWED_TENANTS 白名单（首次上线限定少量租户）。
- 仅审批后执行：LAUNCH_REQUIRE_APPROVAL 必须为真；执行模式为 shadow（沙箱），禁止
  live + mock provider 的"真实资金/业务网关"。
- 全量审计 + 人工复核：LAUNCH_FULL_AUDIT / LAUNCH_MANUAL_REVIEW 必须为真。
- 数据面：STORAGE_BACKEND 必须为 postgres（持久化 + RLS）。

用法：
    python scripts/verify_launch_gate.py [--strict] [--allowed-tenants TENANT-A,TENANT-B]
                         [--execution-mode shadow] [--storage-backend postgres]
    --strict：受限环境下违规即非零退出（默认只打印报告，不失败）。
"""
from __future__ import annotations

import argparse
import os
import sys

# 将仓库根加入 sys.path，允许 `python scripts/verify_launch_gate.py` 直接运行。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import Settings
from src.core.launch_gate import launch_allowlist, verify_launch_gate


def _load_settings(args) -> Settings:
    """从 CLI 覆盖 + 环境变量构造 Settings（优先级：CLI < 环境 < .env）。"""
    env = os.environ.get("ENV", "preview")
    storage_backend = os.environ.get("STORAGE_BACKEND", args.storage_backend)
    allowed = args.allowed_tenants or os.environ.get("LAUNCH_ALLOWED_TENANTS", "")
    execution_mode = args.execution_mode or os.environ.get("EXECUTION_MODE", "shadow")
    return Settings(
        _env_file=None,
        env=env,
        storage_backend=storage_backend,
        launch_allowed_tenants=allowed,
        execution_mode=execution_mode,
        execution_provider=os.environ.get("EXECUTION_PROVIDER", "mock"),
        auth_backend=os.environ.get("AUTH_BACKEND", "real"),
        auth_jwt_secret=os.environ.get("AUTH_JWT_SECRET", "x"),
        launch_require_approval=os.environ.get("LAUNCH_REQUIRE_APPROVAL", "true").lower() != "false",
        launch_full_audit=os.environ.get("LAUNCH_FULL_AUDIT", "true").lower() != "false",
        launch_manual_review=os.environ.get("LAUNCH_MANUAL_REVIEW", "true").lower() != "false",
        launch_gate_strict=args.strict,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="首次上线门控核验")
    parser.add_argument("--strict", action="store_true", help="受限环境违规即非零退出")
    parser.add_argument("--allowed-tenants", default="",
                        help="逗号分隔的上线租户白名单（覆盖环境变量）")
    parser.add_argument("--execution-mode", default="", choices=["", "shadow", "live"])
    parser.add_argument("--storage-backend", default="postgres",
                        choices=["memory", "sqlite", "postgres"])
    parser.add_argument("--env", default="preview", choices=["development", "test", "preview", "production"])
    args = parser.parse_args(argv)

    settings = _load_settings(args)
    report = verify_launch_gate(settings)

    print(f"上线门控报告（ENV={settings.env}, STORAGE={settings.storage_backend}, "
          f"EXECUTION={settings.execution_mode}）")
    print(f"  上线租户白名单: {', '.join(report['allowed_tenants'])}")
    print(f"  违规项（{report['not_ok_count']}）:")
    if report["violations"]:
        for v in report["violations"]:
            print(f"    - {v}")
    else:
        print("    无")

    ok = report["ok"] or not (settings.is_restricted_env and args.strict)
    print(f"\n[{'PASS' if ok else 'FAIL'}] 上线门控")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
