"""RAG 状态隔离与 fail-closed 测试。

验证冻结契约：
- 每次进入 RAG 子图都重置 RAG 运行状态，不残留上轮 checkpoint 的查询/草稿/检测。
- 无文档、schema 非法均 fail-closed（falls_to_error -> 主图 handle_error 转 error）。
- 幻觉检测只读本轮 rag_answer。
"""
from __future__ import annotations

import asyncio

from tests.conftest import bearer
from tests.test_chat_sse import parse_sse as _parse
from src.auth.security import issue_token
from src.core.types import Role
from src.graph.rag import make_rag_graph
from src.llm.mock import MockLLM


def test_rag_fail_closed_when_no_docs():
    llm = MockLLM()
    g = make_rag_graph(llm)
    out = g.invoke({"messages": [{"role": "user", "content": "完全无关的查询内容xyzabc"}],
                    "tenant_id": "TENANT-A"})
    assert out["falls_to_error"] is True
    assert out["reason"] == "no_relevant_documents"


def test_rag_initializes_state_each_run():
    # 每次进入子图都应重置 RAG 运行字段（initialize_rag_run）。
    llm = MockLLM()
    g = make_rag_graph(llm)
    out1 = g.invoke({"messages": [{"role": "user", "content": "退货政策"}], "tenant_id": "TENANT-A"})
    assert out1["rag_answer"] is not None
    assert out1["hallucination_check"] is not None
    # 第二次运行不残留上轮草稿/检测；initialize 重置计数。
    out2 = g.invoke({"messages": [{"role": "user", "content": "退款政策"}], "tenant_id": "TENANT-A"})
    assert out2["rag_attempts"] == 0
    assert out2["hallucination_attempts"] == 0
    # 第二轮草稿基于本轮检索，不来自上一轮。
    assert "退款" in out2["rag_answer"]


class _FakeLLMInvalidCheck(MockLLM):
    def check_hallucination(self, answer, docs):  # noqa: D102
        return {"faithful": "yes"}  # 非法 schema：faithful 非 bool


def test_rag_hallucination_schema_invalid_fail_closed():
    g = make_rag_graph(_FakeLLMInvalidCheck())
    out = g.invoke({"messages": [{"role": "user", "content": "退货政策"}], "tenant_id": "TENANT-A"})
    assert out["falls_to_error"] is True
    assert out["reason"] == "hallucination_check_invalid_output"


def test_policy_error_sse_when_no_docs():
    # 端到端：LLM 幻觉检测 schema 非法 → RAG fail-closed → 主图 handle_error → error 事件。
    from fastapi.testclient import TestClient
    from src.main import create_app
    from src.infrastructure.store import MemoryStore

    store = MemoryStore()
    store.create_tenant("TENANT-A", "租户A")
    store.add_membership("TENANT-A", "USER-001", Role.CUSTOMER)
    app = create_app(store=store, llm=_FakeLLMInvalidCheck(), seed=False)
    with TestClient(app) as client:
        token = issue_token("TENANT-A", "USER-001", Role.CUSTOMER)
        thread_id = client.post("/api/sessions", headers=bearer(token)).json()["thread_id"]
        resp = client.post("/api/chat", json={
            "mode": "start", "thread_id": thread_id,
            "client_request_id": "REQ-RAG1", "message": "退货政策",
        }, headers=bearer(token))
        events = _parse(resp.text)
        names = [e["event"] for e in events]
        assert "error" in names
        assert "done" not in names
        err = next(e for e in events if e["event"] == "error")
        assert err["data"]["retryable"] is False
