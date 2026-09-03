"""首次上线门控：少量租户 / 仅审批后执行 / 全量审计 / 人工复核。

安全红线（Agent 宪法：首次上线限制为少量租户、仅审批后执行、全量审计和人工复核）。
- `launch_allowlist()`：取上线租户白名单（逗号分隔）。
- `tenant_allowed_for_launch()`：租户是否被允许；白名单为空=全放开（全量上线），非空=仅名单内。
- `verify_launch_gate()`：核验上线姿态并给出违规项。受限环境（preview/production）下用
  `--strict` 强制执行（违规即抛错 fail-closed）。本函数也可被 CI / 演练脚本调用以生成记录。
"""
from __future__ import annotations

from typing import Any


def launch_allowlist(settings) -> set[str]:
    return {s.strip() for s in (settings.launch_allowed_tenants or "").split(",") if s.strip()}


def tenant_allowed_for_launch(settings, tenant_id: str) -> bool:
    allow = launch_allowlist(settings)
    if not allow:
        return True
    return tenant_id in allow


def verify_launch_gate(settings) -> dict[str, Any]:
    """核验首次上线姿态，返回结构化报告。

    违规项（任一即 not ok）：
    - 受限环境未配置上线租户白名单（首次上线应限定为少量租户）。
    - 执行模式非法；或 live + mock provider（首次上线只允许 shadow 沙箱，禁真实资金）。
    - 未启用"仅审批后执行""全量审计""人工复核"之一。
    - 数据面不是 PostgreSQL（首次上线应有持久化 + RLS）。
    """
    allow = launch_allowlist(settings)
    violations: list[str] = []
    if settings.is_restricted_env and not allow:
        violations.append(("LAUNCH_ALLOWED_TENANTS 为空：首次上线应为少量租户白名单 "
                           "（全量上线前才清空/全量加入）。"))
    if settings.execution_mode not in {"shadow", "live"}:
        violations.append(f"EXECUTION_MODE={settings.execution_mode} 非法。")
    if settings.execution_mode == "live" and settings.execution_provider == "mock":
        violations.append("EXECUTION_MODE=live 且 execution_provider=mock："
                          "首次上线只允许 shadow 沙箱，禁止真实资金/业务网关。")
    if not settings.launch_require_approval:
        violations.append("LAUNCH_REQUIRE_APPROVAL=false：退款/退货/改址必须仅审批后执行。")
    if not settings.launch_full_audit:
        violations.append("LAUNCH_FULL_AUDIT=false：首次上线要求全量审计留痕。")
    if not settings.launch_manual_review:
        violations.append("LAUNCH_MANUAL_REVIEW=false：首次上线要求人工复核/审批人留痕。")
    if settings.storage_backend != "postgres":
        violations.append(f"STORAGE_BACKEND={settings.storage_backend}："
                          "首次上线应使用 PostgreSQL 数据面（持久化 + RLS）。")
    return {
        "allowed_tenants": sorted(allow) if allow else ["*"],
        "not_ok_count": len(violations),
        "ok": not violations,
        "violations": violations,
    }


def enforce_strict(settings) -> None:
    """严格门控：受限环境下违规即抛 RuntimeError（fail-closed）。"""
    if not settings.launch_gate_strict:
        return
    report = verify_launch_gate(settings)
    if not report["ok"]:
        raise RuntimeError("首次上线门控未通过：\n- " + "\n- ".join(report["violations"]))
