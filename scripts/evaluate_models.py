"""候选模型评测脚本 —— 运行六类通用任务 + 写操作专项评测，产出白名单判别报告。

用法（venv）：
  python scripts/evaluate_models.py                     # 评测默认 self-hosted-model（走本地端点）
  python scripts/evaluate_models.py --model-name MODEL  # 指定要评测的 model id
  python scripts/evaluate_models.py --llm-backend mock  # 用 mock 引擎评测（本地/回归）

输出：`evidence/llm_candidate_eval.json`。`write_op_pass=true` 的 model id 才有资格写入
`HIGH_CONFIDENCE_MODELS`（生产/受限环境还须模型名出现在该报告且经网络白名单与端点可用性验收）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import Settings  # noqa: E402
from src.llm import build_llm  # noqa: E402
from src.llm.eval import WRITE_OP_CASES, evaluate_model  # noqa: E402


def _load_report_settings(model_name: str, llm_backend: str, base_url: str) -> Settings:
    return Settings(
        env="development",
        llm_backend=llm_backend,
        llm_base_url=base_url,
        llm_api_key="sk-local",
        llm_model=model_name,
        llm_allowed_hosts="127.0.0.1,localhost",   # 自托管端点网络白名单
        # 开发环境评测：不要求评测报告路径（评测本身就是产出报告）。
        llm_eval_report_path="",
    )


def _emit_report(evals: list) -> dict:
    report = {}
    for ev in evals:
        report[ev.model] = {
            "write_op_pass": ev.write_op_pass,
            "passed": ev.passed,
            "intent_pass": ev.intent_pass,
            "params_pass": ev.params_pass,
            "faithfulness_pass": ev.faithfulness_pass,
            "low_confidence_pass": ev.low_confidence_pass,
            "endpoint_pass": ev.endpoint_pass,
            "malicious_pass": ev.malicious_pass,
            "any_write_failure": ev.any_write_failure,
            "cases": [r.__dict__ for r in ev.results],
        }
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="候选模型评测（六类通用 + 写操作专项）")
    ap.add_argument("--model-name", default="self-hosted-model", help="要评测的 model id")
    ap.add_argument("--llm-backend", default="openai_compatible", choices=["openai_compatible", "mock"])
    ap.add_argument("--base-url", default="http://127.0.0.1:8021/v1",
                    help="自托管 OpenAI 兼容端点地址（openai_compatible 时使用）")
    ap.add_argument("--output", default="evidence/llm_candidate_eval.json")
    args = ap.parse_args()

    settings = _load_report_settings(args.model_name, args.llm_backend, args.base_url)
    # 端点为自托管本地服务（接入方自管）。若未运行，openai_compatible 建客户端会在调用时才失败，
    # 这里用 make 后的实例跑评测（endpoint 用例通过注入 transport 模拟异常，不影响其它用例的真实契约）。
    llm = build_llm(settings)

    # 对端点场景注入：仅在需要"真实端点可用性"时先探测一次健康；评测的 endpoint 失败用例走注入。
    ev = evaluate_model(llm, simulate_failures=True)
    report = _emit_report([ev])

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    # 控制台摘要 + 白名单建议
    print(f"[model] {args.model_name}  llm_backend={args.llm_backend}")
    print(f"  write_op_pass={ev.write_op_pass}  passed={ev.passed}")
    print(f"  intent={ev.intent_pass} params={ev.params_pass} faithfulness={ev.faithfulness_pass} "
          f"low_confidence={ev.low_confidence_pass} endpoint={ev.endpoint_pass} malicious={ev.malicious_pass}")
    if ev.write_op_pass:
        print(f"  => 该 model id 通过写操作专项评测，可进入 HIGH_CONFIDENCE_MODELS（{args.model_name}）")
    else:
        print(f"  => 该 model id 未通过写操作专项评测，仅可处理查询类任务；敏感写操作带完整状态转人工。")
    print(f"[report] {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
