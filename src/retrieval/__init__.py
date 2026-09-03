"""自托管检索组件包（政策/知识检索）。

完全自托管约束：不接入任何托管向量库/公有 SaaS。缺省使用确定性关键字检索（无外部依赖）；
可选接入自托管 Milvus Lite（本地文件）。检索出口统一为 Retriever.search(tenant_id, query)。
"""
from src.retrieval.base import Retriever, RetrievalError
from src.retrieval.factory import build_retriever
from src.retrieval.keyword import KeywordRetriever
from src.retrieval.milvus import MilvusRetriever

__all__ = ["Retriever", "RetrievalError", "build_retriever", "KeywordRetriever", "MilvusRetriever"]
