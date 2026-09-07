"""真实本地 Qwen3:8B + CrewAI 三条敏感写工具验收探针。

本探针只使用本机 Ollama OpenAI-compatible 端点，不修改生产白名单或评测证据。
它证明的是：模型原生 tool_call -> CrewAI 工具实际执行 -> pending approval ->
审批后 Shadow 执行一次 -> 相同 client_request_id 幂等复用。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Allow direct execution from the repository root or from any working directory.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import Settings
from src.core.types import Role
from src.infrastructure.store import MemoryStore
from src.tools import build_adapter
from src.tools.crewai_adapter import build_crewai_router


def main() -> int:
    if os.environ.get("ALLOW_TEST_WRITE_PROBE") != "1":
        print("Refusing to enable the test-only write capability override. "
              "Set ALLOW_TEST_WRITE_PROBE=1 for this local acceptance probe.", file=sys.stderr)
        return 2
    model = os.environ.get("OLLAMA_MODEL", "qwen3:8b")
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1")
    settings = Settings(
        env="development", crewai_enabled=True, llm_model=model,
        llm_base_url=base_url, llm_allowed_hosts="127.0.0.1,localhost",
        llm_disable_thinking=True, llm_max_tokens=512,
        # Acceptance-only override. It exists solely to exercise the pending-approval
        # boundary against a local model and never changes deployment configuration
        # or evidence/llm_candidate_eval.json.
        high_confidence_models=model,
    )
    store = MemoryStore()
    store.create_tenant("TENANT-A", "演示租户")
    store.add_membership("TENANT-A", "USER-001", Role.CUSTOMER)
    adapter = build_adapter(settings)
    router = build_crewai_router(settings, adapter=adapter, store=store)
    cases = [
        ("refund", {"order_id": "ORD-001", "reason": "商品质量问题",
                       "client_request_id": "probe-refund-001"}),
        ("return_request", {"order_id": "ORD-001", "reason": "商品质量问题",
                               "client_request_id": "probe-return-001"}),
        ("return_address", {"order_id": "ORD-001", "receiver_name": "张三",
                               "phone": "13800138000", "region": "北京市朝阳区",
                               "detail": "幸福路1号", "client_request_id": "probe-address-001"}),
    ]
    evidence: list[dict] = []
    for intent, params in cases:
        ctx = {
            "tenant_id": "TENANT-A", "user_id": "USER-001",
            "role": "customer", "order_id": "ORD-001",
            "thread_id": f"probe-{intent}",
            "client_request_id": params["client_request_id"],
        }
        result = router.run_business_task(intent, ctx, params)
        operation_id = result.get("operation_id")
        approval_id = result.get("approval_id")
        assert result.get("status") == "pending_approval", result
        assert operation_id and approval_id, result
        operation = store.get_operation("TENANT-A", operation_id)
        before_execution = store.get_execution_by_operation("TENANT-A", operation_id)
        assert operation.status.value == "pending" and before_execution is None, operation

        store.decide_approval("TENANT-A", approval_id, "APPROVER-A", True, "", 0.0)
        first = adapter.execute_operation(store, "TENANT-A", "USER-001", "customer", operation_id)
        second = adapter.execute_operation(store, "TENANT-A", "USER-001", "customer", operation_id)
        execution = store.get_execution_by_operation("TENANT-A", operation_id)
        assert first.execution_id == second.execution_id
        assert execution is not None and execution.execution_id == first.execution_id

        duplicate = router.run_business_task(intent, ctx, params)
        assert duplicate.get("operation_id") == operation_id, duplicate
        assert len(store.list_operations("TENANT-A")) == len(evidence) + 1
        final_operation = store.get_operation("TENANT-A", operation_id)
        assert final_operation.status.value == "executed"
        evidence.append({
            "intent": intent, "tool": result.get("tool"),
            "operation_id": operation_id, "approval_id": approval_id,
            "pre_approval_execution_count": 0,
            "post_approval_execution_count": 1,
            "execution_id": first.execution_id,
            "idempotent_operation": True,
            "status_after_approval": final_operation.status.value,
        })
    print(json.dumps({"model": model, "endpoint": base_url, "cases": evidence,
                      "write_paths": "3/3", "approval_bypass": 0,
                      "duplicate_execution": 0}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
