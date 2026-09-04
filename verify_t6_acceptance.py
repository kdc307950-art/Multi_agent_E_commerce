# -*- coding: utf-8 -*-
"""t6 最终验收交叉核对（只读，不改证据）。

覆盖 4 项验收：
  A. 能力矩阵门控：受限环境+空白名单/缺报告 -> fail-closed(空集)；
     capability_ok('self-hosted-model') 在受限且未加入生效白名单时 = False；
     dev + 白名单模型 + write_op_pass=true 报告 -> {model}。
  B. 端到端写操作门控（graph/API 层）：非白名单模型发起退款/退货/改址 ->
     不产生 approval_required / operation_id（fail-closed 转人工）。
  C. 证据可追溯性：llm_candidate_eval.json 全用例/self-hosted-model 判定；
     llm_endpoint_connectivity.json 探测维度；llm_fallback_to_human.json 失败转人工路径。

运行：PYTHONPATH=<项目根> PYTHONUTF8=1 .venv\\Scripts\\python.exe verify_t6_acceptance.py [--endpoint http://127.0.0.1:8001/v1]
"""
import io
import json
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from src.config import Settings  # noqa: E402
from src.llm.capability import (  # noqa: E402
    DEFAULT_DEV_WRITE_MODELS, capability_ok,
    resolve_high_confidence_models, write_capable_models,
)
from src.llm import build_llm  # noqa: E402

EVIDENCE = os.path.join(ROOT, "evidence")
EVAL_REPORT = os.path.join(EVIDENCE, "llm_candidate_eval.json")
CONNECTIVITY = os.path.join(EVIDENCE, "llm_endpoint_connectivity.json")
FALLBACK = os.path.join(EVIDENCE, "llm_fallback_to_human.json")

results = []


def report(item, ok, detail=""):
    results.append((item, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {item}")
    if detail:
        print(f"      {detail}")
    return bool(ok)


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ---- A. 能力矩阵门控 ----
def verify_capability():
    print("== A. 能力矩阵门控 ==")
    # A1: preview + 空白名单 -> 空集(fail-closed)
    s = Settings(env="preview", high_confidence_models="")
    report("A1 preview+空白名单 -> frozenset()",
           resolve_high_confidence_models(s) == frozenset(),
           f"resolve={sorted(resolve_high_confidence_models(s))}")
    report("A1 capability_ok(任意)=False", capability_ok("gpt-4", s) is False)

    # A2: preview + 白名单含X但无报告 -> 空集(fail-closed)
    s2 = Settings(env="preview", high_confidence_models="qwen2.5-max", llm_eval_report_path="")
    report("A2 preview+白名单含X+缺报告 -> frozenset()",
           resolve_high_confidence_models(s2) == frozenset())
    s2b = Settings(env="preview", high_confidence_models="qwen2.5-max",
                   llm_eval_report_path=os.path.join(ROOT, "no_such.json"))
    report("A2b 报告路径不存在 -> frozenset()",
           resolve_high_confidence_models(s2b) == frozenset())

    # A3: 受限 + self-hosted-model 未加入生效白名单 -> capability_ok=False
    #   (dev 显式白名单为空 -> 回落 demo 默认集，self-hosted-model 不在其中 -> False)
    s3 = Settings(env="development", high_confidence_models="",
                  llm_eval_report_path=EVAL_REPORT)
    report("A3 dev默认: capability_ok('self-hosted-model')=False",
           capability_ok("self-hosted-model", s3) is False)
    #   preview + 显式白名单含 self-hosted-model + 报告 write_op_pass=true -> 可写
    s3b = Settings(env="preview", high_confidence_models="self-hosted-model",
                   llm_eval_report_path=EVAL_REPORT)
    report("A3b preview+白名单self-hosted-model+报告true -> {self-hosted-model} 且 ok=True",
           resolve_high_confidence_models(s3b) == frozenset({"self-hosted-model"})
           and capability_ok("self-hosted-model", s3b) is True)
    #   preview + 白名单含 self-hosted-model 但模型 write_op_pass 未在报告中/报告缺 -> 空集
    #   （用受控临时报告标记 write_op_pass=false 验证排除语义）
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"self-hosted-model": {"write_op_pass": False}}, fh, ensure_ascii=False)
    s3c = Settings(env="preview", high_confidence_models="self-hosted-model",
                   llm_eval_report_path=tmp)
    report("A3c preview+白名单模型+报告write_op_pass=false -> frozenset() 且 ok=False",
           resolve_high_confidence_models(s3c) == frozenset()
           and capability_ok("self-hosted-model", s3c) is False)
    os.remove(tmp)

    # A4: build_llm 注入（受限+空白名单）-> 实例 capability_ok=False / is_write_capable(默认名)=False
    s4 = Settings(env="preview", llm_backend="openai_compatible",
                  llm_base_url="http://127.0.0.1:8001/v1", llm_api_key="sk-local",
                  llm_model="gpt-4", llm_allowed_hosts="127.0.0.1,localhost",
                  high_confidence_models="", llm_eval_report_path="")
    llm4 = build_llm(s4)
    report("A4 preview build_llm(gpt-4,空白名单): high_confidence_models=空集",
           llm4.high_confidence_models == frozenset(),
           f"高置信集={sorted(llm4.high_confidence_models)}")
    report("A4 capability_ok=False (gpt-4 受限不可写)", llm4.capability_ok is False)
    report("A4 is_write_capable('gpt-4')=False", llm4.is_write_capable("gpt-4") is False)

    # A5: dev 默认无回归：gpt-4 可写
    s5 = Settings(env="development", high_confidence_models="")
    report("A5 dev默认 capability_ok('gpt-4')=True (无回归)",
           capability_ok("gpt-4", s5) is True and resolve_high_confidence_models(s5) == DEFAULT_DEV_WRITE_MODELS)


# ---- B. 端到端写操作门控 ----
def verify_write_gate(endpoint):
    print("\n== B. 写操作门控（graph/API 层，非白名单模型）==")
    from fastapi.testclient import TestClient
    from langgraph.checkpoint.memory import InMemorySaver
    from src.graph.builder import build_graph
    from src.infrastructure.store import MemoryStore
    from src.core.types import Role
    from src.main import create_app
    try:
        from tests.conftest import bearer
        from tests.test_chat_sse import parse_sse
    except Exception as e:  # pragma: no cover
        # tests.conftest 可能依赖 conftest 层级；此处直接构造 graph 验证，不经 /api
        bearer = parse_sse = None
        print(f"      (tests helper 不可用: {e})")

    store = MemoryStore()
    store.create_tenant("TENANT-A", "a")
    store.add_membership("TENANT-A", "USER-001", Role.CUSTOMER)
    store.add_membership("TENANT-A", "APPROVER-A", Role.APPROVER)

    # B1: graph 层——非白名单模型(dev默认不在集内)写退款 -> falls_to_error, 无 approval
    st = Settings(env="development", llm_backend="openai_compatible",
                  llm_base_url=endpoint, llm_api_key="sk-local",
                  llm_model="self-hosted-model", llm_allowed_hosts="127.0.0.1,localhost")
    llm = build_llm(st)
    report("B1-before: llm.capability_ok=False (self-hosted-model 不在 dev 默认集)",
           llm.capability_ok is False)
    g = build_graph(llm, store, InMemorySaver())
    out = g.invoke({
        "messages": [{"role": "user", "content": "我要退款，订单号 ORD-001"}],
        "tenant_id": "TENANT-A", "user_id": "USER-001", "role": "customer",
        "thread_id": "th", "client_request_id": "c", "model": "self-hosted-model",
    }, config={"configurable": {"thread_id": "th"}})
    report("B1 graph: needs_approval 非 True", out.get("needs_approval") is not True,
           f"needs_approval={out.get('needs_approval')}")
    report("B1 graph: operation_id is None", out.get("operation_id") is None)
    report("B1 graph: falls_to_error=True", out.get("falls_to_error") is True,
           f"reason={out.get('reason')}")

    # B2: API 层——非白名单模型退款 -> 无 approval_required / 有 error / operation_id=None
    try:
        app = create_app(store=store, llm=llm, seed=False, settings=st)
        tok = "mock:TENANT-A:USER-001:customer:abc"
        with TestClient(app) as client:
            sid = client.post("/api/sessions", headers={"Authorization": f"Bearer {tok}"})
            thread_id = sid.json()["thread_id"]
            resp = client.post("/api/chat", json={
                "mode": "start", "thread_id": thread_id, "client_request_id": "REQ-NW",
                "message": "我要退款，订单号 ORD-001"},
                headers={"Authorization": f"Bearer {tok}"})
            events = parse_sse(resp.text) if parse_sse else []
            names = [e["event"] for e in events] if events else []
            report("B2 api: 无 approval_required 事件",
                   ("approval_required" not in names), f"events={names}")
            report("B2 api: 含 error 事件", "error" in names, f"events={names}")
            # 发生 error 事件即 fail-closed
            report("B2 api: 未创建 approval/operation 记录",
                   store.list_approvals("TENANT-A") == [] and store.list_operations("TENANT-A") == [])
    except Exception as e:  # pragma: no cover
        report("B2 api: 端到端 API 验收", False, f"（未能经 /api 复验: {e}）")


# ---- C. 证据可追溯性 ----
def verify_evidence():
    print("\n== C. 证据可追溯性（只读核对）==")
    ev = load(EVAL_REPORT)
    report("C1 eval 报告包含 self-hosted-model", "self-hosted-model" in ev)
    rec = ev.get("self-hosted-model", {})
    report("C2 self-hosted-model.write_op_pass=true", rec.get("write_op_pass") is True)
    report("C3 self-hosted-model.passed=true", rec.get("passed") is True)
    cases = rec.get("cases", [])
    expected_ids = {
        "intent-refund","intent-return","intent-address","intent-order","intent-shipping",
        "intent-policy","intent-complaint","intent-other",
        "params-refund-ok","params-address-ok","params-address-missing","params-return-ok",
        "faithfulness-grounded","faithfulness-hallucinated",
        "lowconf-vague","endpoint-timeout","endpoint-5xx",
        "malicious-inject-addr","malicious-bypass",
        "writeop-refund-nonwhitelist","writeop-return-nonwhitelist","writeop-address-nonwhitelist",
        "writeop-refund-whitelist","writeop-address-whitelist",
    }
    case_ids = {c["case_id"] for c in cases}
    report("C4 eval 含全量 24 用例", len(cases) == 24 and case_ids == expected_ids,
           f"cases={len(cases)} missing={sorted(expected_ids - case_ids)}")
    report("C5 全部用例 passed=true", all(c.get("passed") is True for c in cases),
           f"not_passed={[c['case_id'] for c in cases if not c.get('passed')]}")
    report("C6 any_write_failure=false", rec.get("any_write_failure") is False)

    conn = load(CONNECTIVITY)
    probes = {r["probe"] for r in conn.get("records", [])}
    needed = {"GET /v1/models", "chat/classify_intent", "chat/extract_tool_params",
              "chat/check_hallucination", "chat/rewrite_query", "chat/generate_rag_answer",
              "low_confidence/vague", "timeout/ReadTimeout", "retry/503",
              "invalid_json/_safe_json", "invalid_json/validate_intent_output",
              "unreachable/ConnectError"}
    report("C7 connectivity 覆盖任务要求探测维度", needed <= probes,
           f"missing={sorted(needed - probes)}")
    report("C8 关键失败路径 failure_closed=true",
           all(any(r.get("probe", "").startswith(p) and r.get("failure_closed") is True
                   for r in conn["records"]) for p in
               ("timeout/", "retry/", "invalid_json/", "unreachable/")),
           "timeout/retry/invalid_json/unreachable 均 failure_closed=true")

    fb = load(FALLBACK)
    paths = fb.get("fallback_paths", [])
    output = [p.get("outcome", "") for p in paths]
    report("C9 fallback 覆盖任务失败转人工维度", len(paths) >= 8,
           f"paths={len(paths)}")
    report("C10 所有转人工路径均 operation_id=None 且 approval=None",
           all(p.get("operation_id_is_none") and p.get("approval_none") for p in paths),
           f"outcomes={output}")


def main():
    endpoint = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8001/v1"
    print(f"=== t6 最终验收交叉核对 (endpoint={endpoint}) ===")
    verify_capability()
    verify_write_gate(endpoint)
    verify_evidence()
    npass = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print("\n=== 验收结论 ===")
    for item, ok, _ in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {item}")
    print(f"\n验收项通过: {npass}/{total}")
    return 0 if npass == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
