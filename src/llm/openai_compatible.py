"""本地/内网 OpenAI 兼容端点 LLM（完全自托管，无任何公有 SaaS 依赖）。

- 通过 HTTP 调用自托管端点（vLLM / Ollama 等 OpenAI 兼容实现）的 /chat/completions。
- 结构化输出（意图分类、幻觉检测）要求在返回 JSON，并做严格 schema 校验；
  非法输出一律抛 LLMOutputError，由节点 fail-closed 转人工。
- 端点不可达/超时/5xx 抛 LLMUnavailableError；同类型错误做有界重试 + 指数退避 + jitter。
- 仅使用 httpx（已在依赖中），不引入 openai 官方 SDK，也不接入任何外部托管服务。

注意：本实现仅在配置 LLM_BACKEND=openai_compatible 且 llm_base_url 指向自托管端点时启用。
实际端点可用性需在预发布环境验证；未验证前不得宣称生产可用。
"""
from __future__ import annotations

import json
import random
import re
import time
from typing import Any

import httpx

from src.llm.base import BaseLLM, LLMOutputError, LLMUnavailableError
from src.llm.circuit_breaker import CircuitBreaker, CircuitState
from src.llm.security import EndpointGuard, make_logger, redact, redact_url
from src.llm.validation import (
    validate_hallucination_check,
    validate_intent_output,
)

# 可重试状态码（与《错误处理与回退机制》一致）
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}

_MAX_BACKOFF_BASE = 0.5
_JITTER = 0.2


def _strip_code_fences(text: str) -> str:
    """去掉模型可能裹上的 ```json ``` 代码围栏，便于 json.loads。"""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text


def _safe_json(text: str) -> dict:
    """尝试解析 JSON 对象；失败抛 LLMOutputError。"""
    body = _strip_code_fences(text)
    # 只取第一个 { 到最后一个 } 区间，容忍前后多余文本。
    start = body.find("{")
    end = body.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise LLMOutputError(f"模型未返回 JSON 对象: {text[:120]!r}")
    try:
        data = json.loads(body[start:end + 1])
    except json.JSONDecodeError as exc:
        raise LLMOutputError(f"模型返回非法 JSON: {body[:120]!r}") from exc
    if not isinstance(data, dict):
        raise LLMOutputError("模型返回的 JSON 不是对象")
    return data


class OpenAICompatibleLLM(BaseLLM):
    """调用本地/内网 OpenAI 兼容端点实现的 BaseLLM。"""

    def __init__(self, base_url: str, api_key: str, model: str, *,
                 timeout: float = 10.0, max_retries: int = 2,
                 strict: bool = True, transport: "httpx.BaseTransport | None" = None,
                 allowed_hosts: list[str] | None = None, restricted: bool = False,
                 redact_log: bool = True,
                 high_confidence_models: frozenset[str] | None = None,
                 cb_failure_threshold: int = 5, cb_cooldown_seconds: float = 30.0) -> None:
        super().__init__(high_confidence_models)
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._max_retries = max_retries
        self._strict = strict
        self._guard = EndpointGuard(list(allowed_hosts or []), restricted=restricted)
        if not self._guard.allowed(self._base_url):
            raise LLMUnavailableError(
                f"LLM 端点 {redact_url(repr(self._base_url))} 不在网络白名单内（拒绝访问，fail-closed）")
        self._logger = make_logger("dsh.llm.openai_compatible")
        self._redact = redact_log
        self._cb = CircuitBreaker(failure_threshold=cb_failure_threshold,
                                  cooldown_seconds=cb_cooldown_seconds)
        self._client = httpx.Client(timeout=httpx.Timeout(timeout), trust_env=False,
                                    transport=transport)

    @property
    def model(self) -> str:
        return self._model

    def close(self) -> None:
        self._client.close()

    @property
    def endpoint_allowed(self) -> bool:
        """端点是否通过网络白名单校验（供审计/健康检查）。"""
        return self._guard.allowed(self._base_url)

    @property
    def guard_mode(self) -> str:
        return self._guard.restrict_mode

    @property
    def circuit_state(self) -> str:
        """熔断器当前状态（供审计/健康检查）。"""
        return self._cb.state.value

    # ---- 基础调用（熔断 + 错误分类 + 有界重试）----
    def _chat(self, messages: list[dict], *, response_format: dict | None = None) -> str:
        # 熔断打开 → 快速失败（fail-closed），由节点层转人工，绝不以低档模型降级执行。
        if not self._cb.allow():
            raise LLMUnavailableError(
                "LLM 熔断器打开（端点持续故障），快速失败转人工（不经降级执行）。")
        try:
            content = self._chat_inner(messages, response_format=response_format)
        except LLMUnavailableError:
            self._cb.on_failure()
            raise
        except LLMOutputError:
            # 输出非法：不记入熔断（属模型行为异常），由节点 fail-closed 转人工。
            raise
        self._cb.on_success()
        return content

    def _chat_inner(self, messages: list[dict], *, response_format: dict | None = None) -> str:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": 0.0,
        }
        if response_format is not None:
            payload["response_format"] = response_format

        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = self._client.post(
                    f"{self._base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self._api_key}",
                             "Content-Type": "application/json"},
                    json=payload,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = LLMUnavailableError(f"LLM 端点不可达/超时: {exc}")
                if self._redact:
                    self._logger.warning("LLM 调用失败(attempt=%d): %s", attempt,
                                         redact(str(exc)))
                if attempt < self._max_retries:
                    time.sleep(_backoff(attempt))
                continue

            if resp.status_code in RETRYABLE_STATUS and attempt < self._max_retries:
                if self._redact:
                    self._logger.warning("LLM 可重试失败(attempt=%d, status=%d)",
                                         attempt, resp.status_code)
                time.sleep(_backoff(attempt))
                continue
            if resp.status_code >= 400:
                # 非重试性错误（4xx/内容拒）直接失败 → 转人工。
                raise LLMUnavailableError(
                    f"LLM 端点返回 {resp.status_code}: {redact(resp.text[:200])}"
                )

            try:
                body = resp.json()
                content = body["choices"][0]["message"]["content"]
            except (KeyError, IndexError, ValueError) as exc:
                raise LLMUnavailableError(f"LLM 响应结构非法: {exc}") from exc
            if not isinstance(content, str):
                raise LLMOutputError("LLM message.content 非字符串")
            return content

        # 重试耗尽。
        raise LLMUnavailableError(f"LLM 调用重试耗尽: {last_exc}")

    # ---- 意图分类 ----
    def classify_intent(self, message: str) -> dict:
        prompt = (
            "你是售后工单意图分类器。对以下用户消息输出唯一 JSON 对象，"
            "键为 intent(之一: order/shipping/refund/return_request/return_address/policy/"
            "complaint/other)、confidence(0到1的小数)、order_id(若提到订单号则填ORD-数字格式，"
            "否则null)。不得输出 JSON 以外的任何内容。\n"
            f"用户消息：{message}"
        )
        content = self._chat(
            [{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        data = _safe_json(content)
        return validate_intent_output(data).model_dump()

    # ---- RAG ----
    def rewrite_query(self, query: str) -> str:
        prompt = f"将以下售后咨询改写为更适合检索政策库的查询（保留中文）：{query}"
        content = self._chat([{"role": "user", "content": prompt}]).strip()
        return content or query.strip()

    def grade_relevance(self, query: str, doc: dict) -> int:
        # 由确定性打分（中文 2-gram 重合）完成，避免把相关性交给模型产生不稳定分数。
        import re
        q = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", query)
        hay = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "",
                     doc.get("title", "") + doc.get("content", ""))
        if not q:
            return 0
        grams = {q[i:i + 2] for i in range(len(q) - 1)}
        hits = sum(1 for g in grams if g in hay)
        if hits == 0:
            return 0
        return min(10, int(hits * 2.5)) if hits else 0

    def generate_rag_answer(self, query: str, docs: list[dict]) -> str:
        context = "\n".join(
            f"[{d.get('doc_id', i)}] {d.get('content', '')}" for i, d in enumerate(docs)
        )
        prompt = (
            "仅依据以下检索到的政策资料回答售后咨询；不得编造资料外的内容。\n"
            f"资料：\n{context}\n\n用户问题：{query}"
        )
        return self._chat([{"role": "user", "content": prompt}]).strip()

    def check_hallucination(self, answer: str, docs: list[dict]) -> dict:
        source_text = "\n".join(d.get("content", "") for d in docs)
        prompt = (
            "判断以下售后答案是否完全基于给定的政策资料。仅输出 JSON 对象，键为 "
            "faithful(布尔) 与 issues(字符串数组，列出不支持的要点；忠实时为[])。\n"
            f"资料：\n{source_text}\n\n答案：{answer}"
        )
        content = self._chat(
            [{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        data = _safe_json(content)
        try:
            check = validate_hallucination_check(data)
        except LLMOutputError:
            # 值域非法也应 fail-closed（例如 faithful 变成 "yes"）。
            raise
        return check.model_dump()

    def generate_final_response(self, state: dict, tool_result: str | None = None) -> str:
        intent = state.get("intent")
        if intent == "order":
            return f"订单 {state.get('order_id')} 查询结果：{tool_result}"
        if intent == "shipping":
            return f"物流查询结果：{tool_result}"
        if intent == "complaint":
            return "您的反馈已记录，已为您转人工处理。"
        return tool_result or "需要人工进一步处理。"

    def extract_tool_params(self, intent: str, message: str) -> dict:
        """让模型输出该意图所需的工具参数 JSON；由节点做严格 schema 校验。"""
        if intent == "return_address":
            fields = ("order_id 为ORD-数字格式", "receiver_name 收货人", "phone 11位手机号",
                      "region 省市区", "detail 详细地址")
        elif intent == "refund":
            fields = ("order_id 为ORD-数字格式", "reason 退款理由")
        elif intent == "return_request":
            fields = ("order_id 为ORD-数字格式", "reason 可选退货理由")
        else:
            fields = ("order_id 如提及则填ORD-数字格式，否则null",)
        prompt = (
            f"从以下售后消息提取工具参数，只输出 JSON 对象。字段：{', '.join(fields)}。"
            "缺失且无法确定的字段填 null，不得编造。\n"
            f"消息：{message}"
        )
        content = self._chat(
            [{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        data = _safe_json(content)
        return data


def _backoff(attempt: int) -> float:
    """指数退避 + jitter。"""
    return min(_MAX_BACKOFF_BASE * (2 ** attempt) + random.uniform(0, _JITTER), 3.0)
