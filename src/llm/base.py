"""LLM 抽象基类与错误类型。

所有 LLM 实现（Mock / OpenAI 兼容）必须实现统一的契约方法，并由节点层对输出做严格
schema 校验。结构化输出非法或端点不可用时，一律抛出子类异常，由节点层 fail-closed
转人工，绝不让非法/幻觉化输出进入业务流程。
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class LLMError(Exception):
    """LLM 层的通用错误基类。"""


class LLMUnavailableError(LLMError):
    """端点不可达/超时/5xx 等系统性异常 → 应转人工而非降级执行。"""


class LLMOutputError(LLMError):
    """模型输出非法（schema/值域不满足）→ 必须 fail-closed 转人工。"""


class BaseLLM(ABC):
    """统一 LLM 契约（与 MockLLM / OpenAICompatibleLLM 一致）。

    每个实例携带 `high_confidence_models`（评测驱动白名单，见 src.llm.capability），
    `capability_ok` 据此判定写风险工具门控。节点层写/执行门控一律走这里，不再读
    mock 模块级常量。
    """

    def __init__(self, high_confidence_models: frozenset[str] | None = None) -> None:
        # 默认演示白名单仅用于本地/测试；生产必须由 build_llm 注入评测驱动结果。
        # 注意：受限环境 resolve_high_confidence_models 会返回空 frozenset()（fail-closed），
        # 空集是 falsy，绝不能用 `or DEFAULT_DEV_WRITE_MODELS` 回退成演示默认白名单，
        # 否则会破坏受限环境的写操作 fail-closed 边界。只在显式传 None 时才用演示默认值。
        from src.llm.capability import DEFAULT_DEV_WRITE_MODELS
        self._high_confidence_models = (
            DEFAULT_DEV_WRITE_MODELS if high_confidence_models is None else high_confidence_models
        )

    @property
    @abstractmethod
    def model(self) -> str:
        """能力矩阵所采用的模型名。"""

    @property
    def high_confidence_models(self) -> frozenset[str]:
        """当前生效的写操作白名单（评测驱动）。"""
        return self._high_confidence_models

    @property
    def capability_ok(self) -> bool:
        """写风险工具门控：仅白名单模型可调写工具；非白名单一律拒绝写并转人工。"""
        return self.model in self._high_confidence_models

    def is_write_capable(self, model: str) -> bool:
        """按给定模型名复核写能力（供执行节点二次校验）。"""
        return model in self._high_confidence_models

    @abstractmethod
    def classify_intent(self, message: str) -> dict:
        """返回 {intent, confidence, order_id}；非法输出抛 LLMOutputError。"""

    @abstractmethod
    def rewrite_query(self, query: str) -> str:
        """RAG 查询改写。"""

    @abstractmethod
    def grade_relevance(self, query: str, doc: dict) -> int:
        """相关性评分（0-10）。"""

    @abstractmethod
    def generate_rag_answer(self, query: str, docs: list[dict]) -> str:
        """基于检索文档生成答案草稿。"""

    @abstractmethod
    def check_hallucination(self, answer: str, docs: list[dict]) -> dict:
        """返回 {faithful: bool, issues: list[str]}；非法输出抛 LLMOutputError。"""

    @abstractmethod
    def generate_final_response(self, state: dict, tool_result: str | None = None) -> str:
        """生成最终回复文本。"""

    @abstractmethod
    def extract_tool_params(self, intent: str, message: str) -> dict:
        """从原始用户消息提取该意图所需的工具参数（如改址的地址字段、退款理由）。

        返回的字典需经 validation 层严格校验；非法/缺失即由节点 fail-closed 转人工。
        """
