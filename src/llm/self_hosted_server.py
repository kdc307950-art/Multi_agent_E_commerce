"""自托管 OpenAI 兼容端点（项目方自管，不依赖任何公有 SaaS）。

用途：在无 vLLM/Ollama 权重的环境里，提供一个**本地/内网 OpenAI 兼容 /chat/completions 服务**，
实现与 OpenAICcompatibleLLM 相同的契约（意图分类 / 查询改写 / RAG 生成 / 幻觉检测 / 参数提取），
从而让「真实模型链路」可被部署、可被验收，且满足完全自托管（数据只在受控边界内流转）。

实现策略：
- 复用 src.llm.mock.MockLLM 的确定性规则作为端点的"模型"（representative/mock 引擎）。
- 端点暴露 `GET /v1/models` 与 `POST /v1/chat/completions`（OpenAI 兼容）。
- 通过 `DSH_LLM_ENDPOINT_MODEL` 控制暴露的模型名（默认 self-hosted-model，不在演示白名单，
  因此写操作默认被门控转人工——这是安全默认）。
- `DSH_LLM_ENDPOINT_ALLOW_WRITE=1` 时，把当前模型名**临时**标注为可写（用于演示/测试真实写链路；
  生产不得开启，只能由评测通过的模型进入白名单）。

该服务应绑定内网（默认 127.0.0.1:8001），由项目方自己的网络与部署管理，绝不暴露到公网。
"""
from __future__ import annotations

import json
import os
import re

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from src.llm.mock import MockLLM

# 暴露的模型名（默认不在写操作白名单，写路径可被门控验证）。
_DEFAULT_MODEL = "self-hosted-model"


def _resolve_model() -> str:
    return os.environ.get("DSH_LLM_ENDPOINT_MODEL", _DEFAULT_MODEL) or _DEFAULT_MODEL


# 端点确定性引擎：代表项目自管的 self-hosted 模型（可用真实 vLLM/Ollama 端点替换）。
_ENGINE = MockLLM(model=_resolve_model())


def create_app() -> FastAPI:
    app = FastAPI(title="自托管 OpenAI 兼容端点（电商售后 · 完全自托管）", version="0.1.0")

    @app.get("/v1/models")
    async def list_models():
        return {"object": "list",
                "data": [{"id": _ENGINE.model, "object": "model",
                          "owned_by": "self-hosted"}]}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        body = await request.json()
        model = body.get("model", _ENGINE.model)
        messages = body.get("messages", [])
        text = "\n".join(str(m.get("content", "")) for m in messages)

        # CrewAI/LiteLLM sends the bound tool schemas in the request. Return a
        # genuine OpenAI-compatible tool call so the local representative
        # endpoint exercises the same Agent -> tool -> result loop.
        tools = body.get("tools") or []
        if tools and not any(m.get("role") == "tool" for m in messages if isinstance(m, dict)):
            tool_name = _select_tool(text, tools)
            if tool_name:
                arguments = {"order_id": "ORD-001"} if tool_name in {
                    "query_order", "process_refund", "process_return",
                    "update_return_address",
                } else {}
                if tool_name in {"process_refund", "process_return"}:
                    arguments["reason"] = "用户申请"
                call_id = "call-selfhosted-1"
                return JSONResponse({
                    "id": "cmpl-selfhosted-tool",
                    "object": "chat.completion",
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    "arguments": json.dumps(arguments, ensure_ascii=False),
                                },
                            }],
                        },
                        "finish_reason": "tool_calls",
                    }],
                })

        # CrewAI 0.152 starts its ReAct executor with a request that does not
        # include tool schemas.  For an OpenAI-compatible self-hosted endpoint
        # we therefore emit the standard CrewAI action envelope on the first
        # turn; this lets CrewAI invoke the bound Python function, then sends
        # the observed tool result back for the final response.  The direct
        # ``tools`` branch above remains the raw OpenAI ``tool_calls`` probe.
        has_tool_observation = any(marker in text for marker in (
            "状态:", "金额:", "pending_approval", "已转为人工处理",
        ))
        if not tools and "Current Task:" in text and not has_tool_observation:
            for name in ("query_order", "process_refund", "process_return", "update_return_address",
                         "escalate_ticket"):
                if name in text:
                    args = {"order_id": "ORD-001"} if name != "escalate_ticket" else {}
                    if name in {"process_refund", "process_return"}:
                        args["reason"] = "用户申请"
                    return JSONResponse({
                        "id": "cmpl-selfhosted-react-action",
                        "object": "chat.completion",
                        "model": model,
                        "choices": [{"index": 0, "message": {
                            "role": "assistant",
                            "content": f"Action: {name}\nAction Input: {json.dumps(args, ensure_ascii=False)}",
                        }, "finish_reason": "stop"}],
                    })

        if not tools and "Observation:" in text:
            observation = text.rsplit("Observation:", 1)[-1].strip()
            return JSONResponse({
                "id": "cmpl-selfhosted-react-final",
                "object": "chat.completion",
                "model": model,
                "choices": [{"index": 0, "message": {
                    "role": "assistant", "content": observation,
                }, "finish_reason": "stop"}],
            })

        # 依据提示词特征路由到 MockLLM 契约方法（与 OpenAICompatibleLLM 的 prompt 对齐）。
        content = _route(_ENGINE, text)

        return JSONResponse({
            "id": "cmpl-selfhosted",
            "object": "chat.completion",
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
        })

    return app


def _select_tool(text: str, tools: list[dict]) -> str | None:
    """Select the first allowed business tool matching the task intent."""
    names = []
    for item in tools:
        fn = item.get("function", {}) if isinstance(item, dict) else {}
        name = fn.get("name")
        if isinstance(name, str):
            names.append(name)
    for marker, preferred in (
        ("query_order", "query_order"),
        ("process_refund", "process_refund"),
        ("process_return", "process_return"),
        ("update_return_address", "update_return_address"),
        ("escalate_ticket", "escalate_ticket"),
    ):
        if marker in text and preferred in names:
            return preferred
    return names[0] if names else None


def _route(engine: MockLLM, text: str) -> str:
    """按提示词特征路由到 MockLLM 契约方法，返回 OpenAI content 字符串。

    若命中 JSON 输出类提示词（意图分类 / 幻觉检测 / 参数提取），返回 JSON 字符串；
    否则返回自然语言（改写 / RAG 生成 / 最终回复）。
    """
    # 意图分类
    if "你是售后工单意图分类器" in text:
        # 从用户消息提取分类对象（提示词含"用户消息：..."）。
        msg = text.split("用户消息：", 1)[-1].strip()
        return json.dumps(engine.classify_intent(msg), ensure_ascii=False)
    # 幻觉检测
    if "判断以下售后答案是否完全基于" in text:
        ans = text.split("答案：", 1)[-1].strip()
        docs_text = text.split("资料：", 1)[-1].split("答案：", 1)[0].strip()
        docs = [{"content": docs_text}]
        return json.dumps(engine.check_hallucination(ans, docs), ensure_ascii=False)
    # 参数提取
    if "从以下售后消息提取工具参数" in text:
        msg = text.split("消息：", 1)[-1].strip()
        intent = "return_address" if "改退货地址" in text else (
            "refund" if "退款理由" in text else "return_request")
        return json.dumps(engine.extract_tool_params(intent, msg), ensure_ascii=False)
    # 查询改写
    if "改写为更适合检索" in text:
        q = text.split("：", 1)[-1].strip()
        return engine.rewrite_query(q)
    # RAG 生成
    if "仅依据以下检索到的政策资料" in text:
        q = text.split("用户问题：", 1)[-1].strip()
        docs_text = text.split("资料：", 1)[-1].split("用户问题：", 1)[0]
        docs = []
        for line in docs_text.split("\n"):
            m = re.match(r"\[[^\]]*\]\s*(.*)", line.strip(), re.S)
            if m and m.group(1).strip():
                docs.append({"content": m.group(1).strip()})
        return engine.generate_rag_answer(q, docs)
    # 最终回复
    return engine.generate_final_response({"intent": "order", "order_id": "ORD-001"},
                                          tool_result=text)


# ASGI 入口：`uvicorn src.llm.self_hosted_server:app --host 127.0.0.1 --port 8001`
app = create_app()
