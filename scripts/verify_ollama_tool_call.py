"""Probe a local Ollama OpenAI-compatible endpoint for native tool calls.

This proves protocol compatibility only; it does not grant write capability or
claim that CrewAI can safely execute sensitive operations.
"""
from __future__ import annotations

import json
import os

import httpx


def main() -> int:
    model = os.environ.get("OLLAMA_MODEL", "qwen3:4b")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "查询订单 ORD-001，只调用 query_order 工具"}],
        "tools": [{"type": "function", "function": {
            "name": "query_order", "description": "查询订单",
            "parameters": {"type": "object", "properties": {
                "order_id": {"type": "string"}}, "required": ["order_id"]}}}],
        "tool_choice": {"type": "function", "function": {"name": "query_order"}},
        "temperature": 0, "think": False, "max_tokens": 512,
    }
    calls = []
    message = {}
    for _ in range(3):
        response = httpx.post("http://127.0.0.1:11434/v1/chat/completions",
                              json=payload, timeout=45)
        response.raise_for_status()
        message = response.json()["choices"][0]["message"]
        calls = message.get("tool_calls") or []
        if calls:
            break
    if not calls:
        raise SystemExit("[FAIL] Ollama returned no tool_calls")
    call = calls[0]["function"]
    args = json.loads(call["arguments"])
    if call["name"] != "query_order" or args.get("order_id") != "ORD-001":
        raise SystemExit(f"[FAIL] unexpected tool call: {call}")
    print(f"[PASS] {model} returned query_order(ORD-001) via native tool_calls")
    print("[INFO] Protocol compatibility only; write whitelist remains unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
