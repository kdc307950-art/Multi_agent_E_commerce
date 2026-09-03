"""检索组件工厂：按 settings.retrieval_backend 构造自托管组件。

- keyword（默认）：确定性关键字检索，无外部依赖。
- milvus：自托管 Milvus Lite（本地文件），需环境安装 pymilvus，否则构造失败（快速失败）。

不允许任何"未配置检索即改用第三方/SaaS 兜底"的行为。
"""
from __future__ import annotations

from src.config import Settings
from src.retrieval.base import Retriever
from src.retrieval.keyword import KeywordRetriever
from src.retrieval.milvus import MilvusRetriever
from src.tools import mock_data


def build_retriever(settings: Settings) -> Retriever:
    backend = (settings.retrieval_backend or "keyword").strip().lower()
    if backend == "keyword":
        return KeywordRetriever(
            knowledge_base=mock_data.KNOWLEDGE_BASE,
            keywords_map=mock_data.POLICY_KEYWORDS,
            top_k=settings.retrieval_top_k,
        )
    if backend == "milvus":
        return MilvusRetriever(
            uri=settings.milvus_uri,
            db_name=settings.milvus_db_name,
            collection=settings.milvus_collection,
            top_k=settings.retrieval_top_k,
            dim=settings.milvus_embedding_dim,
            source_docs=mock_data.KNOWLEDGE_BASE,
        )
    raise ValueError(f"未知检索后端: {backend!r}（可选 keyword | milvus）")


__all__ = ["build_retriever", "Retriever", "KeywordRetriever", "MilvusRetriever"]
