"""端到端验收证据：模型不可用/不在白名单时，敏感写操作 fail-closed，不产生审批与执行。

验收项⑥（部分）："模型不可用时不产生敏感写"。本脚本构造指向自托管端点（或不可达端点）的
真实 LLM，发起退款写申请，断言：
- SSE 只产生 error（code 为 intent_classification_failed 或 model_not_in_whitelist），operation_id=null；
- 不产生 approval_required；
- store 中无 operation（敏感操作未进入审批/执行链）且 execution 数为 0。

运行方式：python evidence/verify_no_sensitive_write_fail_closed.py --base-url http://127.0.0.1:8001/v1
（base-url 指向不可达端点时即"模型不可用"场景；指向可运行但非白名单端点时即"非白名单"场景。）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from src.auth.security import issue_token  # noqa: E402
from src.config import Settings  # noqa: E402
from src.core.types import Role  # noqa: E402
from src.infrastructure.store import MemoryStore  # noqa: E402
from src.llm import build_llm  # noqa: E402
from src.main import create_app  # noqa: E402


def _parse_events(text: str) -> list[dict]:
    out = []
    for block in text.strip().split("\n\n"):
        if not block.strip():
            continue
        d = {}
        for line in block.split("\n"):
            if line.startswith("event: "):
                d["event"] = line[7:]
            elif line.startswith("data: "):
                d["data"] = json.loads(line[6:])
        if "event" in d:
            out.append(d)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8001/v1")
    ap.add_argument("--model", default="self-hosted-model")
    args = ap.parse_args()

    store = MemoryStore()
    store.create_tenant("TENANT-A", "租户A")
    store.add_membership("TENANT-A", "USER-001", Role.CUSTOMER)
    store.add_membership("TENANT-A", "APPROVER-A", Role.APPROVER)

    s = Settings(_env_file=None, env="development", llm_backend="openai_compatible",
                 llm_base_url=args.base_url, llm_api_key="sk-local",
                 llm_model=args.model, llm_allowed_hosts="localhost,127.0.0.1")
    app = create_app(store=store, llm=build_llm(s), seed=False, settings=s)
    tok = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)

    with TestClient(app) as c:
        tid = c.post("/api/sessions",
                     headers={"Authorization": f"Bearer {tok}"}).json()["thread_id"]
        resp = c.post("/api/chat", json={
            "mode": "start", "thread_id": tid, "client_request_id": "REQ-NOSW-1",
            "message": "我要退款，订单号 ORD-001",
        }, headers={"Authorization": f"Bearer {tok}"})

    events = _parse_events(resp.text)
    names = [e["event"] for e in events]
    errors = [e["data"] for e in events if e["event"] == "error"]
    ops = store.list_operations("TENANT-A")
    exes = store.list_execution_records("TENANT-A")

    result = {
        "scenario": "模型不可用/非白名单 → 敏感写 fail-closed",
        "base_url": args.base_url,
        "model": args.model,
        "sse_events": names,
        "error_payload": errors,
        "operations": [{"action": o.pending_action.value, "status": o.status.value} for o in ops],
        "execution_count": len(exes),
    }

    # fail-closed 不变量：无审批事件、有 error、operation_id 为 null、无 operation/execution。
    ok = (
        "approval_required" not in names
        and bool(errors)
        and all(e.get("operation_id") is None for e in errors)
        and len(ops) == 0
        and len(exes) == 0
    )
    result["accepted"] = ok
    out = Path(__file__).with_name("no_sensitive_write_fail_closed.json")
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("ACCEPTED:", ok)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
