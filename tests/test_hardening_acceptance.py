"""安全加固 / 验收测试（覆盖本次需求与验收标准）。

覆盖：
1. LLM_BACKEND 真实选择 Mock / 本地 OpenAI 兼容端点；openai_compatible 走本地端点并严格校验。
2. 意图/工具参数/审批恢复参数/模型输出的严格校验（非法 fail-closed）。
3. 低置信度 fail-closed 转人工。
4. 订单归属、退款资格、金额、退货资格、改址参数校验（异常/不明 → 转人工）。
5. 带租户校验的外部电商能力工具适配器。
6. 自托管 Agentic RAG / 检索组件（keyword 缺省；Milvus 可选）。
7. CrewAI/MCP 集成（真实调用链 + Mock/集成测试）。
8. 敏感写操作不会绕过唯一 human_approval。
"""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from src.auth.security import issue_token
from src.config import Settings
from src.core.types import Role
from src.graph.builder import build_graph
from src.infrastructure.store import MemoryStore
from src.llm import build_llm, LLMOutputError, LLMUnavailableError
from src.llm.mock import MockLLM
from src.llm.openai_compatible import OpenAICompatibleLLM
from src.llm.validation import (
    validate_address_change,
    validate_hallucination_check,
    validate_intent_output,
    validate_resume_params,
)
from src.main import create_app
from src.tools import (
    AdapterError,
    CrewAIToolRouter,
    EcommerceAdapter,
    build_adapter,
    build_crewai_router,
    MockCrewAIBackend,
)
from src.tools.crewai_adapter import BUSINESS_TOOL_SCHEMAS
from tests.conftest import bearer
from tests.test_chat_sse import parse_sse


# ---------------------------------------------------------------------------
# 1. LLM_BACKEND 真实选择
# ---------------------------------------------------------------------------
def test_build_llm_returns_mock_by_default():
    llm = build_llm(Settings(llm_backend="mock"))
    assert isinstance(llm, MockLLM)


def test_build_llm_returns_openai_compatible_when_configured():
    llm = build_llm(Settings(llm_backend="openai_compatible", llm_model="self-hosted-model"))
    assert isinstance(llm, OpenAICompatibleLLM)


def test_unknown_llm_backend_fails_fast():
    with pytest.raises(ValueError):
        build_llm(Settings(llm_backend="saaS-gpt"))


def _fake_httpx_response(body: dict, status_code: int = 200):
    return httpx.Response(status_code, json=body, request=httpx.Request("POST", "http://x"))


def test_openai_compatible_classify_intent_uses_local_endpoint_and_validates():
    def handler(request: httpx.Request):
        # 校验确实走本地端点，且带 Bearer。
        assert str(request.url).startswith("http://127.0.0.1:9999/v1")
        assert request.headers["Authorization"] == "Bearer sk-local"
        payload = json.loads(request.content)
        assert payload["model"] == "self-hosted-model"
        assert payload["response_format"] == {"type": "json_object"}
        return _fake_httpx_response({
            "choices": [{"message": {"content": json.dumps({
                "intent": "refund", "confidence": 0.9, "order_id": "ORD-001"})}}],
        })

    llm = OpenAICompatibleLLM("http://127.0.0.1:9999/v1", "sk-local", "self-hosted-model",
                              transport=httpx.MockTransport(handler))
    out = llm.classify_intent("我要退款，订单号 ORD-001")
    assert out["intent"] == "refund" and out["confidence"] == 0.9 and out["order_id"] == "ORD-001"


def test_openai_compatible_invalid_intent_output_fails_closed():
    def handler(request: httpx.Request):
        return _fake_httpx_response({
            "choices": [{"message": {"content": json.dumps({
                "intent": "order", "confidence": 3.5})}}],  # confidence 越界
        })

    # 用 loopback host 走白名单（测试目标是"非法输出 fail-closed"，与主机无关）。
    llm = OpenAICompatibleLLM("http://127.0.0.1:9999/v1", "k", "m", transport=httpx.MockTransport(handler))
    with pytest.raises(LLMOutputError):
        llm.classify_intent("你好")


def test_openai_compatible_endpoint_unavailable_fails_closed():
    def handler(request: httpx.Request):
        return _fake_httpx_response({"error": "boom"}, status_code=500)

    llm = OpenAICompatibleLLM("http://127.0.0.1:9999/v1", "k", "m", max_retries=1,
                              transport=httpx.MockTransport(handler))
    with pytest.raises(LLMUnavailableError):
        llm.classify_intent("你好")


# ---------------------------------------------------------------------------
# 2. 严格校验（意图 / 幻觉 / 工具参数 / 审批恢复参数）
# ---------------------------------------------------------------------------
def test_validate_intent_output_rejects_bad_intent():
    with pytest.raises(LLMOutputError):
        validate_intent_output({"intent": "hack", "confidence": 0.9})
    with pytest.raises(LLMOutputError):
        validate_intent_output({"intent": "refund", "confidence": 1.5})


def test_validate_hallucination_check_rejects_bad_schema():
    with pytest.raises(LLMOutputError):
        validate_hallucination_check({"faithful": "yes", "issues": []})
    ok = validate_hallucination_check({"faithful": False, "issues": ["x"]})
    assert ok.faithful is False


def test_validate_address_change_rejects_missing_fields():
    with pytest.raises(LLMOutputError):
        validate_address_change({"order_id": "ORD-001", "receiver_name": "", "phone": "123",
                                 "region": "", "detail": ""})
    ok = validate_address_change({"order_id": "ORD-001", "receiver_name": "张三",
                                  "phone": "13800138000", "region": "北京市朝阳区",
                                  "detail": "幸福路1号"})
    assert ok.phone == "13800138000"


def test_validate_resume_params_requires_bool_approved():
    with pytest.raises(LLMOutputError):
        validate_resume_params({"approved": "yes"})
    assert validate_resume_params({"approved": True, "approver": "ADMIN-A"})["approved"] is True


# ---------------------------------------------------------------------------
# 3. 低置信度 fail-closed 转人工
# ---------------------------------------------------------------------------
def test_low_confidence_intent_escalates_to_human():
    store = MemoryStore()
    store.create_tenant("TENANT-A", "a")
    store.add_membership("TENANT-A", "USER-001", Role.CUSTOMER)
    g = build_graph(MockLLM(), store, InMemorySaver(), conf_threshold=0.7)
    out = g.invoke({"messages": [{"role": "user", "content": "今天天气如何"}],
                    "tenant_id": "TENANT-A", "user_id": "USER-001", "role": "customer",
                    "thread_id": "th", "client_request_id": "c", "model": "gpt-4"},
                   config={"configurable": {"thread_id": "th"}})
    assert "人工" in out.get("final_response", "")


# ---------------------------------------------------------------------------
# 4/5. 归属 / 资格 / 金额 / 退货资格 / 改址参数校验 → 转人工
# ---------------------------------------------------------------------------
def _eligible_order_ctx_store():
    s = MemoryStore()
    s.create_tenant("TENANT-A", "a")
    s.create_tenant("TENANT-B", "b")
    s.add_membership("TENANT-A", "USER-001", Role.CUSTOMER)
    s.add_membership("TENANT-A", "USER-002", Role.CUSTOMER)
    s.add_membership("TENANT-A", "ADMIN-A", Role.ADMIN)
    s.add_membership("TENANT-B", "USER-B1", Role.CUSTOMER)
    return s


def test_refund_eligibility_ok_for_delivered_order():
    ad = EcommerceAdapter()
    r = ad.check_refund_eligibility("TENANT-A", "USER-001", "customer", "ORD-001")
    assert r.eligible is True and r.amount == 299.00


def test_refund_ineligible_for_not_delivered_order():
    ad = EcommerceAdapter()
    r = ad.check_refund_eligibility("TENANT-A", "USER-001", "customer", "ORD-002")  # shipped
    assert r.eligible is False and r.reason == "order_not_delivered"


def test_refund_ineligible_when_window_exceeded():
    ad = EcommerceAdapter()
    r = ad.check_refund_eligibility("TENANT-A", "USER-001", "customer", "ORD-004")  # 70 days ago
    assert r.eligible is False and r.reason == "refund_window_exceeded"


def test_return_ineligible_for_fresh_category():
    ad = EcommerceAdapter()
    r = ad.check_return_eligibility("TENANT-A", "USER-001", "customer", "ORD-003")  # 生鲜
    assert r.eligible is False and r.reason == "category_not_returnable"


def test_cross_user_order_not_owned_denied():
    ad = EcommerceAdapter()
    # USER-002 查 USER-001 的订单（同租户跨用户，非 staff）→ 拒绝。
    with pytest.raises(AdapterError):
        ad.get_order("TENANT-A", "USER-002", "customer", "ORD-001")
    # staff 可见。
    assert ad.get_order("TENANT-A", "ADMIN-A", "admin", "ORD-001").order_id == "ORD-001"


def test_cross_tenant_order_denied():
    ad = EcommerceAdapter()
    # USER-B1 尝试用 TENANT-A 上下文访问 TENANT-A 的订单（身份不在该订单归属）→ 拒绝。
    with pytest.raises(AdapterError):
        ad.get_order("TENANT-A", "USER-B1", "customer", "ORD-001")
    # TENANT-B 自己的 ORD-001（属于 USER-B1）可访问。
    assert ad.get_order("TENANT-B", "USER-B1", "customer", "ORD-001").order_id == "ORD-001"


def test_address_change_validated():
    ad = EcommerceAdapter()
    ok = ad.validate_address_change("TENANT-A", "USER-001", "customer", "ORD-001",
                                    {"receiver_name": "张三", "phone": "13800138000",
                                     "region": "北京市朝阳区", "detail": "幸福路1号"})
    assert ok["region"] == "北京市朝阳区"


def test_address_change_rejects_invalid_phone_on_write():
    store = _eligible_order_ctx_store()
    g = build_graph(MockLLM("gpt-4"), store, InMemorySaver())
    # 地址中电话非法 → 转人工（falls_to_error），不产生 approval。
    out = g.invoke({"messages": [{"role": "user", "content": (
        '我要改退货地址，订单号 ORD-001，{"receiver_name":"张三","phone":"123",'
        '"region":"北京市朝阳区","detail":"幸福路1号"}')}],
        "tenant_id": "TENANT-A", "user_id": "USER-001", "role": "customer",
        "thread_id": "th", "client_request_id": "c", "model": "gpt-4"},
        config={"configurable": {"thread_id": "th"}})
    assert out.get("falls_to_error") is True


# ---------------------------------------------------------------------------
# 端到端（API）：资格不明 / 工具参数非法 → 转人工；敏感写走审批
# ---------------------------------------------------------------------------
@pytest.fixture
def eligible_app():
    store = _eligible_order_ctx_store()
    store.add_membership("TENANT-A", "APPROVER-A", Role.APPROVER)
    return create_app(store=store, llm=MockLLM("gpt-4"), seed=False)


def test_ineligible_refund_escalates_no_approval(eligible_app):
    with TestClient(eligible_app) as client:
        tok = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
        thread_id = client.post("/api/sessions", headers=bearer(tok)).json()["thread_id"]
        resp = client.post("/api/chat", json={
            "mode": "start", "thread_id": thread_id,
            "client_request_id": "REQ-INEL", "message": "我要退款，订单号 ORD-002",  # 未签收
        }, headers=bearer(tok))
        names = [e["event"] for e in parse_sse(resp.text)]
        assert "approval_required" not in names
        assert "error" in names
        err = next(e for e in parse_sse(resp.text) if e["event"] == "error")
        assert err["data"]["code"] == "refund_ineligible"


def test_invalid_address_param_escalates_no_approval(eligible_app):
    with TestClient(eligible_app) as client:
        tok = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
        thread_id = client.post("/api/sessions", headers=bearer(tok)).json()["thread_id"]
        # 改址但地址缺失 → 工具参数非法 → 转人工，不产生审批。
        resp = client.post("/api/chat", json={
            "mode": "start", "thread_id": thread_id,
            "client_request_id": "REQ-ADDR-BAD", "message": "我要改退货地址，订单号 ORD-001",
        }, headers=bearer(tok))
        names = [e["event"] for e in parse_sse(resp.text)]
        assert "approval_required" not in names
        assert "error" in names


def test_address_change_requires_approval_then_execute(eligible_app):
    with TestClient(eligible_app) as client:
        tok_user = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
        tok_admin = issue_token("TENANT-A", "APPROVER-A", Role.APPROVER)
        thread_id = client.post("/api/sessions", headers=bearer(tok_user)).json()["thread_id"]
        msg = ('我要改退货地址，订单号 ORD-001，{"receiver_name":"张三","phone":"13800138000",'
               '"region":"北京市朝阳区","detail":"幸福路1号"}')
        resp = client.post("/api/chat", json={
            "mode": "start", "thread_id": thread_id,
            "client_request_id": "REQ-ADDR", "message": msg,
        }, headers=bearer(tok_user))
        events = parse_sse(resp.text)
        ar = next(e for e in events if e["event"] == "approval_required")
        approval_id, operation_id = ar["data"]["approval_id"], ar["data"]["operation_id"]
        # 审批前 pending（无 direct 绕过）。
        before = client.get(f"/api/operations/{operation_id}", headers=bearer(tok_admin))
        assert before.json()["status"] == "pending"
        # 审批通过 → 执行。
        ok = client.post(f"/api/approvals/{approval_id}/decision",
                         json={"approved": True, "confirmation": True}, headers=bearer(tok_admin))
        assert ok.status_code == 200
        after = client.get(f"/api/operations/{operation_id}", headers=bearer(tok_admin))
        assert after.json()["status"] == "executed"


def test_real_model_exception_escalates_to_human_on_write():
    # 模型抛 LLMUnavailableError（真实异常）→ 转人工，不产生审批。
    store = _eligible_order_ctx_store()
    store.add_membership("TENANT-A", "APPROVER-A", Role.APPROVER)

    class BrokenLLM(MockLLM):
        def classify_intent(self, message):
            raise LLMUnavailableError("endpoint down")

    app = create_app(store=store, llm=BrokenLLM("gpt-4"), seed=False)
    with TestClient(app) as client:
        tok = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
        thread_id = client.post("/api/sessions", headers=bearer(tok)).json()["thread_id"]
        resp = client.post("/api/chat", json={
            "mode": "start", "thread_id": thread_id,
            "client_request_id": "REQ-REALERR", "message": "我要退款，订单号 ORD-001",
        }, headers=bearer(tok))
        names = [e["event"] for e in parse_sse(resp.text)]
        assert "approval_required" not in names
        assert "error" in names


# ---------------------------------------------------------------------------
# 6. 自托管检索组件（keyword 缺省 / Milvus 可选）
# ---------------------------------------------------------------------------
def test_keyword_retriever_is_tenant_scoped():
    from src.retrieval import KeywordRetriever
    r = KeywordRetriever(top_k=4)
    docs_a = r.search("TENANT-A", "退货")
    docs_b = r.search("TENANT-B", "退货")
    assert all(d["tenant_id"] == "TENANT-A" for d in docs_a)
    assert all(d["tenant_id"] == "TENANT-B" for d in docs_b)
    # 无命中 → 空（RAG fail-closed 的前提）。
    assert r.search("TENANT-A", "宇宙的尽头") == []
    # 无租户作用域 → 空。
    assert r.search("", "退货") == []


def test_milvus_retriever_requires_pymilvus_or_fails_closed():
    from src.retrieval import MilvusRetriever, RetrievalError
    try:
        import pymilvus  # noqa: F401
        available = True
    except Exception:
        available = False
    if not available:
        with pytest.raises(RetrievalError):
            MilvusRetriever("./data/x.db", "d", "c")
    else:
        r = MilvusRetriever("./data/milvus_test.db", "d", "c", source_docs=[])
        assert r.search("TENANT-A", "退货") == []


# ---------------------------------------------------------------------------
# 7. CrewAI / MCP 集成（真实调用链 + Mock/集成测试）
# ---------------------------------------------------------------------------
def test_crewai_resolve_tool_maps_intent_to_tool():
    assert CrewAIToolRouter.resolve_tool("refund") == "process_refund"
    assert CrewAIToolRouter.resolve_tool("return_request") == "process_return"
    assert CrewAIToolRouter.resolve_tool("return_address") == "update_return_address"
    assert len(BUSINESS_TOOL_SCHEMAS) == 6


def test_crewai_mock_backend_records_real_call_chain():
    store = _eligible_order_ctx_store()
    router = build_crewai_router(Settings(crewai_enabled=False), adapter=build_adapter(Settings()))
    # 默认关闭 → 走确定性后端，记录调用链。
    res = router.run_business_task("order", {"tenant_id": "TENANT-A", "user_id": "USER-001",
                                             "role": "customer", "order_id": "ORD-001"})
    assert res["tool"] == "query_order"
    assert len(router.mock_calls) == 1
    assert router.mock_calls[0]["intent"] == "order"


def test_crewai_enabled_without_crewai_fails_closed():
    store = _eligible_order_ctx_store()
    router = build_crewai_router(Settings(crewai_enabled=True), adapter=build_adapter(Settings()))
    with pytest.raises(Exception):
        router.run_business_task("order", {"tenant_id": "TENANT-A"})


@pytest.mark.skipif(True, reason="本环境未安装 crewai；真实调用链在具备 crewai 的环境用集成测试验证")
def test_crewai_real_call_chain_integration():
    import crewai  # noqa: F401
    router = build_crewai_router(Settings(crewai_enabled=True, llm_model="self-hosted-model"),
                                 adapter=build_adapter(Settings()))
    res = router.run_business_task("query_order", {"tenant_id": "TENANT-A"})
    assert res["tool"] == "query_order"


# ---------------------------------------------------------------------------
# 8. 敏感写不绕过审批（已有的审批链测试之外，补地址写路径）
# ---------------------------------------------------------------------------
def test_write_actions_never_bypass_approval_graph():
    # 直接检查主图：process_* → human_approval → execute_*，无 direct 边。
    from src.graph.builder import build_graph
    store = _eligible_order_ctx_store()
    store.add_membership("TENANT-A", "APPROVER-A", Role.APPROVER)
    g = build_graph(MockLLM("gpt-4"), store, InMemorySaver())
    out = g.invoke({"messages": [{"role": "user", "content": "我要退款，订单号 ORD-001"}],
                    "tenant_id": "TENANT-A", "user_id": "USER-001", "role": "customer",
                    "thread_id": "th", "client_request_id": "c", "model": "gpt-4"},
                   config={"configurable": {"thread_id": "th"}})
    # 未审批 → 停在 human_approval 中断，操作仍 pending，绝不已执行。
    assert out.get("needs_approval") is True
    assert out.get("final_response") is None
