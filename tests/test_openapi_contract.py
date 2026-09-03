"""OpenAPI 契约锁死测试。

目标：确认后端 OpenAPI 文档（自动生成）与冻结 REST 契约一致，从而保证前端/测试/文档
引用的接口路径与方法在代码层同步。契约见《生产环境架构设计》§3.5。
"""
from __future__ import annotations


def test_openapi_contract_endpoints(app):
    o = app.openapi()
    paths = o["paths"]

    # AI 流唯一入口 post /api/chat
    assert "post" in paths["/api/chat"]

    # 会话（统一 /api 前缀）
    assert paths["/api/sessions"].get("post") is not None
    assert paths["/api/sessions"].get("get") is not None
    assert "get" in paths["/api/sessions/{thread_id}"]
    assert "get" in paths["/api/sessions/{thread_id}/messages"]

    # 审批唯一决策入口，审批列表/详情
    assert "get" in paths["/api/approvals"]
    assert "get" in paths["/api/approvals/{approval_id}"]
    assert "post" in paths["/api/approvals/{approval_id}/decision"]

    # 操作状态查询
    assert "get" in paths["/api/operations/{operation_id}"]


def test_openapi_chat_body_is_discriminated_union(app):
    o = app.openapi()
    schemas = o.get("components", {}).get("schemas", {})
    assert "ChatStart" in schemas
    assert "ChatResume" in schemas
    # 判别式字段：mode 必须为 const "start" / "resume"。
    assert schemas["ChatStart"]["properties"]["mode"]["const"] == "start"
    assert schemas["ChatResume"]["properties"]["mode"]["const"] == "resume"
    # 请求体应引用 start/resume（判别式联合）。
    chat_refs = []
    rb = o["paths"]["/api/chat"]["post"].get("requestBody")
    if rb:
        schema = rb["content"]["application/json"]["schema"]
        chat_refs = [r.get("$ref", "") for r in schema.get("oneOf", [])]
    assert any("ChatStart" in r for r in chat_refs) and any("ChatResume" in r for r in chat_refs)


def test_openapi_rejects_client_tenant_field(app):
    o = app.openapi()
    schemas = o["components"]["schemas"]
    # ChatStart 使用 extra=forbid，拒绝客户端覆盖 tenant_id/user_id。
    chat_start = schemas.get("ChatStart", {})
    props = set(chat_start.get("properties", {}).keys())
    assert "mode" in props and "thread_id" in props
    assert "client_request_id" in props and "message" in props
    # tenant_id / user_id 不允许出现在请求 schema 属性中。
    assert "tenant_id" not in props
    assert "user_id" not in props


def test_healthz_is_unauthenticated_same_origin(client):
    """免认证健康端点在 /api 前缀下同源可达（供 nginx/健康检查探测），可不暴露租户数据。"""
    r = client.get("/api/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
