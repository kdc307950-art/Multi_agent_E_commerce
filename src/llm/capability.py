"""模型能力矩阵（写操作白名单）策略 —— 评测驱动，不硬编码真实模型名。

设计红线（见《错误处理与回退机制》§3.2 / Agent 宪法）：
- 写风险工具（process_refund / process_return / update_return_address）只允许
  `HIGH_CONFIDENCE_MODELS` 白名单内模型调用；判断依据是「模型名显式白名单」，不是分数阈值。
- 只有明确标注为**真实权重**、`whitelist_eligible=true` 且通过写操作专项评测
  （write_op_pass=true，见 src/llm/eval 与 scripts/evaluate_models.py）的 model id 才能进入白名单；
  Mock 和代表性端点评测一律拒绝写操作并转人工。
- 受限环境（preview/production）**强制评测门控**：白名单模型必须同时出现在评测报告中且
  write_op_pass=true，否则 fail-closed（无任何模型可写，全部转人工）。

本模块是能力矩阵的**唯一权威解析口**。`BaseLLM.capability_ok` / 节点层门控都走这里，
不再读取 mock.py 的模块级常量。mock.py 仅保留 dev 演示默认值以兼容本地/测试。
"""
from __future__ import annotations

import json
import os

# 开发/测试演示默认白名单（仅 mock 演示与本地测试用；**不是**生产结论）。
# 这些名字只是让 mock 的写路径能走通审批，便于验证流程；真实自托管模型必须经评测后
# 通过配置注入 `HIGH_CONFIDENCE_MODELS`。任何生产/受限环境都不允许落在这些默认值上。
DEFAULT_DEV_WRITE_MODELS: frozenset[str] = frozenset({
    "gpt-4", "gpt-4-turbo", "claude-3-opus", "qwen2.5-max",
})


def _split_csv(value: str) -> set[str]:
    return {s.strip() for s in (value or "").split(",") if s.strip()}


def _load_eval_report(path: str) -> dict[str, dict]:
    """读取写操作专项评测报告 JSON，并规范化。

    期望结构：{"<model_id>": {"write_op_pass": bool, ...}, ...}
    文件缺失/非法时返回 {}（调用方按「未评测」处理 → fail-closed）。
    """
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict] = {}
    for mid, rec in data.items():
        if isinstance(mid, str) and isinstance(rec, dict):
            out[mid] = rec
    return out


def resolve_high_confidence_models(settings) -> frozenset[str]:
    """解析生效的写操作白名单（评测驱动）。

    - base：配置 `HIGH_CONFIDENCE_MODELS` 显式白名单；为空且处于开发/测试则回退到演示默认值，
      受限环境为空则空集（fail-closed）。
    - report：真实权重、可进入白名单且写操作专项评测通过的模型集合。
    - 最终生效 = base ∩ report_pass；受限环境且未提供报告 → 空集（fail-closed）。
    """
    base = _split_csv(getattr(settings, "high_confidence_models", ""))
    restricted = bool(getattr(settings, "is_restricted_env", False))

    if not base:
        # 未配置显式白名单：受限环境 fail-closed；开发/测试回退到演示默认值（可写演示）。
        if restricted:
            return frozenset()
        return DEFAULT_DEV_WRITE_MODELS

    # 配置了显式白名单：按评测报告门控。
    report = _load_eval_report(getattr(settings, "llm_eval_report_path", ""))
    # `write_op_pass` 仅表示评测用例通过；Mock/代表性端点评测不能证明真实
    # 权重模型能力。只有报告明确声明真实权重来源并标记可进入白名单时才放行。
    passed = {
        mid for mid, rec in report.items()
        if rec.get("write_op_pass") is True
        and rec.get("evaluation_backend") == "real_weight"
        and rec.get("whitelist_eligible") is True
    }

    if restricted and not report:
        # 受限环境必须提供评测报告；缺失即 fail-closed（不把未评测模型放入可写名单）。
        return frozenset()

    # 生效白名单 = 显式白名单 ∩ 评测通过集合（开发环境显式白名单但无报告时，保留显式值以兼容）。
    if not passed:
        # 显式提供了报告但没有真实权重资格时，开发环境也不得回退到可写白名单。
        # 仅在完全没有报告路径/内容的本地演示场景保留兼容回退。
        report_path = getattr(settings, "llm_eval_report_path", "")
        return frozenset(base) if not restricted and not report_path else frozenset()
    return frozenset(base) & passed


def capability_ok(model: str, settings) -> bool:
    """写操作门控：模型必须位于评测驱动白名单，否则拒绝写并转人工。"""
    return model in resolve_high_confidence_models(settings)


def write_capable_models(settings) -> set[str]:
    """返回当前生效的可写模型名集合（供审计/上报）。"""
    return set(resolve_high_confidence_models(settings))


# --- 兼容：保留一个模块常量名，供无 settings 的局部读取；仅代表开发/测试演示默认值。 ---
HIGH_CONFIDENCE_MODELS: frozenset[str] = DEFAULT_DEV_WRITE_MODELS
