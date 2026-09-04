# -*- coding: utf-8 -*-
"""t5 (acceptance-engineer) 证据采集：LLM 端点可用性 + 能力矩阵解析 + 运行时门控。

如实记录：真实自托管模型端点是否存在、能力矩阵/写门控解析、执行模式与自托管边界。
只读 + 受控探测，不改任何生产运行时。
"""
import io
import json
import os
import sys
import time
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)

from src.config import Settings
from src.llm.capability import (
    resolve_high_confidence_models,
    capability_ok,
    write_capable_models,
    DEFAULT_DEV_WRITE_MODELS,
)

OUT = os.path.join(ROOT, "evidence", "prod-go-live", "acceptance-engineer",
                   "LLM_GATEWAY_EVIDENCE.json")

EVIDENCE_REPORT = os.path.join(ROOT, "evidence", "llm_candidate_eval.json")


def probe_endpoint(base_url: str, timeout: float = 3.0) -> dict:
    """探测 OpenAI 兼容端点 GET /v1/models 是否可达（真实可用性）。"""
    url = base_url.rstrip("/") + "/models"
    ok, status, body, err = False, None, None, None
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status = r.status
            body = r.read().decode("utf-8", errors="replace")[:400]
            ok = status == 200
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
    return {"endpoint": base_url, "probe": "GET /v1/models", "ok": ok,
            "status_code": status, "body": body, "error": err}


def main() -> int:
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "generated_by": "acceptance-engineer (t5 prod-go-live)",
        "note": "诚实探测真实外部端点 + 能力矩阵/写门控解析 + 执行模式。凡依赖真实模型/网关端点而未具备的，如实标注 BLOCKED。",
    }

    # ---- 1. 真实 LLM 端点可用性探测（host 侧） ----
    report["llm_endpoint_probe_host"] = probe_endpoint("http://127.0.0.1:8001/v1")

    # ---- 2. 评测报告当前状态（权威性） ----
    if os.path.exists(EVIDENCE_REPORT):
        with open(EVIDENCE_REPORT, encoding="utf-8") as fh:
            ev = json.load(fh)
        report["eval_report_path"] = EVIDENCE_REPORT
        report["eval_report_models"] = {
            k: {"write_op_pass": (v.get("write_op_pass") if isinstance(v, dict) else None),
                "n_cases": (len(v.get("cases", [])) if isinstance(v, dict) else 0)}
            for k, v in ev.items()
        }
    else:
        report["eval_report_path"] = EVIDENCE_REPORT
        report["eval_report_models"] = None

    # ---- 3. 能力矩阵/写门控解析（若干场景） ----
    def cap(env, hcm, report_path):
        s = Settings(env=env, high_confidence_models=hcm, llm_eval_report_path=report_path)
        wl = resolve_high_confidence_models(s)
        wl2 = write_capable_models(s)
        return {"env": env, "high_confidence_models": hcm,
                "eval_report_path": report_path,
                "resolved_whitelist": sorted(wl),
                "write_capable_models": sorted(wl2),
                "capability_ok(self-hosted-model)": capability_ok("self-hosted-model", s),
                "capability_ok(self-hosted-demo)": capability_ok("self-hosted-demo", s),
                "capability_ok(gpt-4)": capability_ok("gpt-4", s)}

    report["capability_scenarios"] = {
        "dev_default": cap("development", "", EVIDENCE_REPORT),
        "preview_empty_whitelist": cap("preview", "", EVIDENCE_REPORT),
        "preview_whitelist_no_report": cap("preview", "self-hosted-demo", ""),
        "preview_self_hosted_demo_whitelist_and_report": cap(
            "preview", "self-hosted-demo", EVIDENCE_REPORT),
        "preview_self_hosted_model_whitelist_and_report": cap(
            "preview", "self-hosted-model", EVIDENCE_REPORT),
    }
    report["DEFAULT_DEV_WRITE_MODELS"] = sorted(DEFAULT_DEV_WRITE_MODELS)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
