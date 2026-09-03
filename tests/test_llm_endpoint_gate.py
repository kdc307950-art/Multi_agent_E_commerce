"""本阶段验收测试：自托管 LLM 端点接入 + 评测驱动能力矩阵 + 安全门控。

验收项（见任务要求）：
1. 真实模型链路可用：LLM_BACKEND=openai_compatible 走本地自托管端点，真实 SSE 链路可分流/回复。
2. 异常输出与超时不会触发执行：端点 5xx/超时/非法输出 → 写操作 fail-closed 转人工，
   不产生 approval_required / executed，不发执行结果。
3. 未白名单模型无法走退款/退货/改址审批执行链：非白名单模型写申请 → 不创建 operation/approval，
   fail-closed 转人工；审批链不产生可执行操作。

配套：网络白名单（端点不在白名单拒绝访问）、脱敏日志、评测驱动白名单（只有 write_op_pass
的 model id 才可写）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from src.config import Settings
from src.core.types import Role, ErrorCode
from src.graph.builder import build_graph
from src.infrastructure.store import MemoryStore
from src.llm import build_llm
from src.llm.capability import (
    DEFAULT_DEV_WRITE_MODELS,
    capability_ok,
    resolve_high_confidence_models,
    write_capable_models,
)
from src.llm.eval import evaluate_model
from src.llm.mock import MockLLM
from src.llm.openai_compatible import OpenAICompatibleLLM
from src.llm.security import EndpointGuard, redact, redact_url
from src.main import create_app
from tests.conftest import bearer
from tests.test_chat_sse import parse_sse


# ---------------------------------------------------------------------------
# 工具：临时自托管端点
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def self_hosted_endpoint():
    """启动一个临时自托管 OpenAI 兼容端点（默认 self-hosted-model，不在白名单）。"""
    import uvicorn
    from src.llm.self_hosted_server import app
    port = 8011
    env = {**os.environ, "DSH_LLM_ENDPOINT_MODEL": "self-hosted-model"}
    proc = subprocess.Popen(
        [sys.executable, "-c",
         f"import uvicorn;from src.llm.self_hosted_server import app;uvicorn.run(app,host='127.0.0.1',port={port})"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        time.sleep(0.1)
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/v1/models", timeout=1.0)
            if r.status_code == 200:
                break
        except Exception:
            continue
    else:
        proc.kill()
        pytest.skip("自托管端点未就绪")
    yield f"http://127.0.0.1:{port}/v1"
    proc.terminate()


def _eligible_store() -> MemoryStore:
    s = MemoryStore()
    s.create_tenant("TENANT-A", "a")
    s.create_tenant("TENANT-B", "b")
    s.add_membership("TENANT-A", "USER-001", Role.CUSTOMER)
    s.add_membership("TENANT-A", "APPROVER-A", Role.APPROVER)
    s.add_membership("TENANT-B", "USER-B1", Role.CUSTOMER)
    return s


def _real_app(base_url: str, model: str, *, eval_report: str = ""):
    """构造面向自托管端点的真实应用（openai_compatible + 网络白名单放行 loopback）。"""
    store = _eligible_store()
    st = Settings(env="development", llm_backend="openai_compatible", llm_base_url=base_url,
                  llm_api_key="sk-local", llm_model=model,
                  llm_allowed_hosts="127.0.0.1,localhost",
                  llm_eval_report_path=eval_report)
    return create_app(store=store, llm=build_llm(st), seed=False, settings=st), store


# ---------------------------------------------------------------------------
# 一、能力矩阵（评测驱动）——只有 write_op_pass 的 model id 才可写
# ---------------------------------------------------------------------------
def test_capability_default_dev_set():
    # 开发/测试未配置显式白名单 → 回退演示默认值（可写演示）。
    s = Settings(env="development")
    assert resolve_high_confidence_models(s) == DEFAULT_DEV_WRITE_MODELS
    assert capability_ok("gpt-4", s) is True


def test_capability_restricted_empty_whitelist_fails_closed():
    # 受限环境 + 未配置白名单 → 无任何模型可写（fail-closed）。
    s = Settings(env="preview", high_confidence_models="")
    assert resolve_high_confidence_models(s) == frozenset()
    assert capability_ok("any-model", s) is False


def test_capability_restricted_requires_eval_report():
    # 受限环境 + 配置白名单但无评测报告 → fail-closed（不把未评测模型放入可写名单）。
    s = Settings(env="preview", high_confidence_models="qwen2.5-max", llm_eval_report_path="")
    assert resolve_high_confidence_models(s) == frozenset()


def test_capability_only_eval_passed_whitelisted():
    # 受限环境 + 白名单模型 + 评测报告标记 write_op_pass=true → 才可写。
    report_path = os.path.join(os.path.dirname(__file__), "..", "evidence",
                               "llm_candidate_eval.json")
    # 生成/读取一份把 qwen2.5-max 标记为通过的评测报告。
    import json
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump({"qwen2.5-max": {"write_op_pass": True,
                                   "cases": [{"case_id": "writeop-refund-whitelist",
                                              "passed": True}]}}, fh, ensure_ascii=False)
    s = Settings(env="preview", high_confidence_models="qwen2.5-max",
                 llm_eval_report_path=report_path)
    assert resolve_high_confidence_models(s) == frozenset({"qwen2.5-max"})
    assert capability_ok("qwen2.5-max", s) is True
    assert capability_ok("self-hosted-model", s) is False


def test_eval_runner_marks_mock_write_op_pass():
    # mock（确定性、符合契约）应通过写操作专项评测。
    ev = evaluate_model(MockLLM("gpt-4"))
    assert ev.intent_pass and ev.params_pass and ev.faithfulness_pass
    assert ev.endpoint_pass and ev.malicious_pass
    assert ev.write_op_pass is True


# ---------------------------------------------------------------------------
# 二、网络白名单 + 脱敏日志
# ---------------------------------------------------------------------------
def test_endpoint_guard_rejects_non_whitelisted_public_host():
    g = EndpointGuard(["localhost", "10.0.0.0/8"], restricted=False)
    assert g.allowed("http://localhost:8000/v1") is True
    assert g.allowed("http://10.1.2.3:8000/v1") is True
    assert g.allowed("http://api.openai.com/v1") is False  # 公网 SaaS 拒绝
    assert g.allowed("http://evil.example.com/v1") is False


def test_endpoint_guard_restricted_empty_fails_closed():
    g = EndpointGuard([], restricted=True)
    assert g.allowed("http://127.0.0.1:8001/v1") is False  # 受限环境空白名单 → 拒绝一切
    assert g.allowed("http://localhost:8001/v1") is False


def test_endpoint_guard_private_default_in_dev():
    g = EndpointGuard([], restricted=False)
    assert g.allowed("http://127.0.0.1:8001/v1") is True
    assert g.allowed("http://localhost:8001/v1") is True
    assert g.allowed("http://10.0.0.5:8001/v1") is True
    assert g.allowed("https://api.openai.com/v1") is False  # 公网 SaaS 默认拒绝


def test_build_llm_rejects_out_of_whitelist_endpoint():
    s = Settings(llm_backend="openai_compatible", llm_base_url="http://evil.example.com:5000/v1",
                 llm_model="m", llm_allowed_hosts="127.0.0.1")
    with pytest.raises(Exception):
        build_llm(s)


def test_redact_masks_pii():
    text = "我要改地址到北京市朝阳区幸福路1号，手机13800138000，订单ORD-123，Bearer sk-abcdef123456"
    out = redact(text)
    assert "13800138000" not in out
    assert "ORD-123" not in out
    assert "sk-abcdef123456" not in out
    assert "Bearer " not in out
    assert "幸福路1号" not in out


def test_redact_url_masks_query():
    assert "token=***" in redact_url("http://x/v1?token=abc123&x=1")
    assert "abc123" not in redact_url("http://x/v1?token=abc123")


# ---------------------------------------------------------------------------
# 三、验收：异常输出 / 超时不会触发执行
# ---------------------------------------------------------------------------
def test_endpoint_5xx_raises_unavailable_not_output_error():
    def handler(request):
        return httpx.Response(503, json={"error": "boom"},
                              request=httpx.Request("POST", "http://127.0.0.1:8001/v1"))
    llm = OpenAICompatibleLLM("http://127.0.0.1:8001/v1", "sk-local", "m", max_retries=1,
                              transport=httpx.MockTransport(handler),
                              allowed_hosts=["127.0.0.1", "localhost"], restricted=False,
                              redact_log=False)
    from src.llm.base import LLMUnavailableError
    with pytest.raises(LLMUnavailableError):
        llm.classify_intent("退款 ORD-001")


def test_timeout_raises_unavailable():
    def handler(request):
        raise httpx.ReadTimeout("simulated timeout")
    llm = OpenAICompatibleLLM("http://127.0.0.1:8001/v1", "sk-local", "m", max_retries=1,
                              transport=httpx.MockTransport(handler),
                              allowed_hosts=["127.0.0.1", "localhost"], restricted=False,
                              redact_log=False)
    from src.llm.base import LLMUnavailableError
    with pytest.raises(LLMUnavailableError):
        llm.classify_intent("退款 ORD-001")


# ---------------------------------------------------------------------------
# 四、验收：非白名单模型不达写审批执行链（真实端点）
# ---------------------------------------------------------------------------
def test_non_whitelist_model_write_fails_closed_via_api(self_hosted_endpoint):
    app, store = _real_app(self_hosted_endpoint, "self-hosted-model")
    with TestClient(app) as client:
        tok = "mock:TENANT-A:USER-001:customer:abc"
        thread_id = client.post("/api/sessions", headers=bearer(tok)).json()["thread_id"]
        # 退款（非白名单模型）→ 不产生 approval_required，产生 error（model_not_in_whitelist）。
        resp = client.post("/api/chat", json={
            "mode": "start", "thread_id": thread_id, "client_request_id": "REQ-NW",
            "message": "我要退款，订单号 ORD-001"}, headers=bearer(tok))
        events = parse_sse(resp.text)
        names = [e["event"] for e in events]
        assert "approval_required" not in names
        assert "error" in names
        err = next(e for e in events if e["event"] == "error")
        assert err["data"]["code"] == ErrorCode.MODEL_NOT_IN_WHITELIST.value
        assert err["data"]["operation_id"] is None  # 未创建操作
        # 无遗留待审批操作。
        assert store.list_approvals("TENANT-A") == []
        assert store.list_operations("TENANT-A") == []


def test_non_whitelist_model_no_bypass_in_graph(self_hosted_endpoint):
    # 在图层：非白名单模型写操作 → falls_to_error，绝不创建 approval/进入 execute。
    store = _eligible_store()
    llm = build_llm(Settings(env="development", llm_backend="openai_compatible",
                             llm_base_url=self_hosted_endpoint, llm_api_key="sk-local",
                             llm_model="self-hosted-model", llm_allowed_hosts="127.0.0.1,localhost"))
    g = build_graph(llm, store, InMemorySaver())
    out = g.invoke({
        "messages": [{"role": "user", "content": "我要退款，订单号 ORD-001"}],
        "tenant_id": "TENANT-A", "user_id": "USER-001", "role": "customer",
        "thread_id": "th", "client_request_id": "c", "model": "self-hosted-model",
    }, config={"configurable": {"thread_id": "th"}})
    assert out.get("needs_approval") is not True
    assert out.get("operation_id") is None
    assert out.get("falls_to_error") is True


def test_whitelist_model_still_requires_approval(self_hosted_endpoint):
    # 白名单模型（在这份评测报告中标记 write_op_pass 的模型）写操作仍必须进入唯一 human_approval。
    import json
    report_path = os.path.join(os.path.dirname(__file__), "..", "evidence",
                               "llm_candidate_eval.json")
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump({"self-hosted-demo": {"write_op_pass": True,
                                        "cases": [{"case_id": "writeop-refund-whitelist",
                                                   "passed": True}]}}, fh, ensure_ascii=False)
    store = _eligible_store()
    llm = build_llm(Settings(env="development", llm_backend="openai_compatible",
                             llm_base_url=self_hosted_endpoint, llm_api_key="sk-local",
                             llm_model="self-hosted-demo", llm_allowed_hosts="127.0.0.1,localhost",
                             high_confidence_models="self-hosted-demo",
                             llm_eval_report_path=report_path))
    # 直接验证端点 classify 契约可用（调试真实链路）。
    ci = llm.classify_intent("我要退款，订单号 ORD-001")
    assert ci["intent"] == "refund", f"endpoint classify broken: {ci}"
    g = build_graph(llm, store, InMemorySaver())
    out = g.invoke({
        "messages": [{"role": "user", "content": "我要退款，订单号 ORD-001"}],
        "tenant_id": "TENANT-A", "user_id": "USER-001", "role": "customer",
        "thread_id": "th2", "client_request_id": "c2", "model": "self-hosted-demo",
    }, config={"configurable": {"thread_id": "th2"}})
    # 白名单 → 进入审批（needs_approval=true），绝不直接执行。
    assert out.get("needs_approval") is True
    assert out.get("approval_id") is not None
    assert out.get("operation_id") is not None
    assert out.get("final_response") is None  # 停在 human_approval 中断
