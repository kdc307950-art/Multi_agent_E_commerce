"""检索组件抽象（完全自托管）。

Retriever 是项目唯一允许的政策/知识检索入口，替代直接调用 mock_data 或任何外部向量库。
所有实现都必须：
- 以 tenant_id 为强制作用域；禁止无租户条件的查询。
- 返回的文档仅含当前租户数据，并携带可验证的 provenance（doc_id/来源）。
- 检索为空时返回空列表，由 RAG 子图 fail-closed 转人工（绝不用跨租户/默认上下文兜底）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class RetrievalError(Exception):
    """检索组件系统性错误（如 Milvus 不可用）。"""


class Retriever(ABC):
    """检索接口：policy 文档检索。"""

    @abstractmethod
    def search(self, tenant_id: str, query: str, top_k: int | None = None) -> list[dict]:
        """按租户检索政策文档，返回 [{doc_id, title, content, tenant_id, score}]。
        top_k 缺省用组件默认值；跨租户/缺租户一律拒绝或忽略非本租户文档。
        """
