"""确定性关键字检索（默认、完全自托管、无外部依赖）。

作为缺省检索组件，用于本地开发/单元测试与无 Milvus 时的自托管回退。基于租户过滤 +
中文关键词命中评分，确定性可复现。检索为空时返回空列表（由 RAG 子图 fail-closed）。
"""
from __future__ import annotations

import re

from src.retrieval.base import Retriever

_CLEAN = re.compile(r"[\s，。、,:：?!？]")


class KeywordRetriever(Retriever):
    """基于关键字的自托管检索组件（替代直接访问 mock_data 的检索）。"""

    def __init__(self, knowledge_base: list[dict] | None = None,
                 keywords_map: dict[str, list[str]] | None = None,
                 top_k: int = 4) -> None:
        # knowledge_base: [{tenant_id, doc_id, title, content}]
        # keywords_map: {doc_id: [keyword, ...]}
        self._kb = knowledge_base if knowledge_base is not None else _default_kb()
        self._keywords = keywords_map if keywords_map is not None else _default_keywords()
        self._top_k = top_k

    def _docs_for(self, tenant_id: str) -> list[dict]:
        return [d for d in self._kb if d.get("tenant_id") == tenant_id]

    def search(self, tenant_id: str, query: str, top_k: int | None = None) -> list[dict]:
        if not tenant_id:
            return []
        k = top_k if top_k is not None else self._top_k
        docs = self._docs_for(tenant_id)
        q = _CLEAN.sub("", query)
        if not q:
            # 无有效查询：返回该租户全部政策（仍仅为租户作用域）作默认。
            scored = [(0, d) for d in docs]
        else:
            scored = []
            for d in docs:
                kw = self._keywords.get(d.get("doc_id"), [])
                hits = sum(1 for kw_word in kw if kw_word in q)
                # 仅关键字命中的文档进入候选（与既有语义一致），命中数作为评分。
                if hits > 0:
                    scored.append((hits, d))
        scored.sort(key=lambda x: -x[0])
        result = []
        for score, d in scored[:k]:
            result.append({
                "tenant_id": d["tenant_id"],
                "doc_id": d["doc_id"],
                "title": d["title"],
                "content": d["content"],
                "score": score,
            })
        return result


def _content_hits(query: str, content: str) -> int:
    q = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", query)
    hay = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", content)
    if not q or not hay:
        return 0
    grams = {q[i:i + 2] for i in range(len(q) - 1)}
    return sum(1 for g in grams if g in hay)


def _default_kb() -> list[dict]:
    from src.tools import mock_data
    return mock_data.KNOWLEDGE_BASE


def _default_keywords() -> dict[str, list[str]]:
    from src.tools import mock_data
    return mock_data.POLICY_KEYWORDS
