"""可插拔 Mock LLM（本地规则实现，完全自托管、无外部依赖）。

用途：让状态机、政策查询、幻觉检测在无本地/内网模型端点时也能进行**本地/Mock 验证**，
用于单元测试与本地演示。它不产生真实市场结论，只用于验证流程与契约。

真实场景应切换到本地/内网 OpenAI 兼容端点（vLLM/Ollama），需配置 llm_base_url/model。
mock 的默认 model 设为 "gpt-4" 处于开发演示白名单，以使写操作测试能走通审批路径；
真实自托管模型名若不在白名单，写操作将被门控转人工（安全行为）。
"""
from __future__ import annotations

import re

from src.llm.base import BaseLLM
from src.llm.validation import (
    validate_hallucination_check,
    validate_intent_output,
)
from src.tools import mock_data

from src.llm import capability as _capability  # noqa: E402

# 开发/测试演示白名单（与 capability.DEFAULT_DEV_WRITE_MODELS 一致；仅供 mock 演示。
# 生产写操作白名单由 config + 评测报告决定，见 src.llm.capability；本常量仅供无 settings 的局部兼容使用）。
HIGH_CONFIDENCE_MODELS: set[str] = set(_capability.DEFAULT_DEV_WRITE_MODELS)

ORDER_ID_PATTERN = re.compile(r"(ORD[-_]?\d+)", re.IGNORECASE)


class MockLLM(BaseLLM):
    """确定性规则 LLM 实现，全部返回可预测结果，便于测试与本地演示。"""

    def __init__(self, model: str = "gpt-4",
                 high_confidence_models: frozenset[str] | None = None) -> None:
        super().__init__(high_confidence_models)
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    # ---- 意图分类 ----
    def classify_intent(self, message: str) -> dict:
        text = message.lower()
        order_id = None
        m = ORDER_ID_PATTERN.search(message)
        if m:
            order_id = m.group(1).upper()

        if "改址" in text or "改地址" in text or "改退货地址" in text:
            intent, confidence = "return_address", 0.95
        elif "退款" in text:
            intent, confidence = "refund", 0.92
        elif any(k in text for k in ("政策", "规则", "怎么退", "可以退", "支持退", "能退", "退货流程")):
            intent, confidence = "policy", 0.9
        elif "退货" in text:
            intent, confidence = "return_request", 0.93
        elif "投诉" in text:
            intent, confidence = "complaint", 0.9
        elif any(k in text for k in ("物流", "到哪里", "轨迹", "快递", "签收")):
            intent, confidence = "shipping", 0.9
        elif "订单" in text or ("查" in text and order_id):
            intent, confidence = "order", 0.9
        elif any(k in text for k in ("政策", "怎么退", "规则", "支持", "可以退")):
            intent, confidence = "policy", 0.9
        else:
            intent, confidence = "other", 0.5
        # 严格校验（确定性输出必然合法）；非法即抛，供上层 fail-closed。
        return validate_intent_output(
            {"intent": intent, "confidence": confidence, "order_id": order_id}
        ).model_dump()

    # ---- RAG：改写 ----
    def rewrite_query(self, query: str) -> str:
        return query.strip()

    # ---- RAG：相关性评分 (0-10) ----
    def grade_relevance(self, query: str, doc: dict) -> int:
        """基于中文 2-gram 重合度打分：query 与文档标题/内容的重合越多分越高。"""
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

    # ---- RAG：生成答案 ----
    def generate_rag_answer(self, query: str, docs: list[dict]) -> str:
        if not docs:
            return ""
        # 从文档拼接，避免编造；仅依据源文档。
        parts = [d.get("content", "") for d in docs]
        return "\n".join(parts)

    # ---- RAG：幻觉检测 ----
    def check_hallucination(self, answer: str, docs: list[dict]) -> dict:
        """Mock：仅当答案为空或答案内容与源文档完全不匹配时判为不忠实。

        返回值经严格 schema 校验（faithful: bool, issues: list[str]），非法即抛。
        """
        issues: list[str] = []
        if not answer:
            issues.append("missing_rag_answer")
            return validate_hallucination_check(
                {"faithful": False, "issues": issues}
            ).model_dump()
        # 抽取源文档中的关键片段，检查答案是否都来自源文档（简单探针）。
        source_text = " ".join(d.get("content", "") for d in docs)
        # 若答案含源文档中没有的实质性句子，判不忠实（Phase 1 简化探针）。
        sentences = [s.strip() for s in re.split(r"[。！？\n]", answer) if s.strip()]
        for s in sentences:
            core = s[:10]
            if core and core not in source_text:
                issues.append(f"not_supported: {s[:12]}")
        if issues:
            return validate_hallucination_check(
                {"faithful": False, "issues": issues}
            ).model_dump()
        return validate_hallucination_check(
            {"faithful": True, "issues": []}
        ).model_dump()

    # ---- 意图分类后的回复（非 RAG 场景）----
    def generate_final_response(self, state: dict, tool_result: str | None = None) -> str:
        intent = state.get("intent")
        if intent == "order":
            return f"订单 {state.get('order_id')} 查询结果：{tool_result}"
        if intent == "shipping":
            return f"物流查询结果：{tool_result}"
        if intent == "complaint":
            return "您的反馈已记录，已为您转人工处理。"
        return tool_result or "需要人工进一步处理。"

    # ---- 工具参数提取 ----
    def extract_tool_params(self, intent: str, message: str) -> dict:
        """确定性地从消息提取该意图所需结构化参数。

        改址需收货人/电话/地区/详址；退款/退货需 order_id + 可选理由。缺字段或非法由
        校验层 fail-closed 转人工。优先识别消息中内嵌的 JSON 地址片段。
        """
        import json
        parsed: dict = {}
        m = ORDER_ID_PATTERN.search(message)
        if m:
            parsed["order_id"] = m.group(1).upper()
        # 若消息内含 JSON 对象，取第一个作为参数。
        start = message.find("{")
        if start != -1:
            end = message.rfind("}")
            if end > start:
                try:
                    data = json.loads(message[start:end + 1])
                    if isinstance(data, dict):
                        parsed.update(data)
                except Exception:
                    pass
        if intent == "return_address":
            if "order_id" not in parsed:
                return parsed  # 交由校验层判缺字段 → fail-closed
            self._extract_address(message, parsed)
            return parsed
        if intent == "refund":
            parsed.setdefault("reason", (message or "").strip()[:200] or "申请退款")
            return parsed
        if intent == "return_request":
            parsed.setdefault("reason", (message or "").strip()[:200] or None)
            return parsed
        return parsed

    @staticmethod
    def _extract_address(message: str, parsed: dict) -> None:
        # 收货人
        for kw in ("收货人", "收件人", "联系人"):
            if kw in message:
                seg = message.split(kw, 1)[1]
                seg = re.split(r"[，,。；;\s]", seg, 1)[0]
                parsed.setdefault("receiver_name", seg.strip())
                break
        # 电话
        phone = re.search(r"1[3-9]\d{9}", re.sub(r"[\s\-]", "", message))
        if phone:
            parsed.setdefault("phone", phone.group(0))
        # 地区 + 详址：取"地址/改到/寄到"后到"电话"前的内容，前段省市区，后段作为详址。
        for kw in ("地址", "改到", "寄到", "收货地址"):
            if kw in message:
                seg = message.split(kw, 1)[1]
                seg = re.split(r"电话|手机|联系", seg, 1)[0]
                seg = re.sub(r"[，,。；;\s]+$", "", seg).strip()
                if seg:
                    # 取前 10 字作为地区（省市区），其余作为详址（尽力而为）。
                    parsed.setdefault("region", seg[:10])
                    parsed.setdefault("detail", seg[10:] or seg)
                break
