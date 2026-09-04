"""检索组件工厂：按 settings 构造自托管组件（政策语料来自受控业务数据源）。

设计目标（见《工具调用与集成》§2.3 / Agent 宪法 §6.5）：
- 政策文档的**唯一权威来源**是 `BusinessDataSource`（mock 或 PostgreSQL），
  `build_retriever` 不再直接读 `mock_data.KNOWLEDGE_BASE`。
- `DataSourceRetriever` 把 `Retriever.search(tenant_id, query)` 委托给数据源的政策检索，
  保证租户作用域与完全自托管；检索为空返回 []，由 RAG 子图 fail-closed 转人工。
- `KeywordRetriever` / `MilvusRetriever` 保留为独立检索组件（本地/测试/未来接入向量检索），
  但它们也应以数据源的政策文档为语料来源，而不是直接读 mock_data。

不允许任何"未配置检索即改用第三方/SaaS 兜底"的行为。
"""
from __future__ import annotations

from src.config import Settings
from src.retrieval.base import Retriever
from src.retrieval.keyword import KeywordRetriever
from src.retrieval.milvus import MilvusRetriever
from src.tools.data_source import BusinessDataSource, build_data_source


class DataSourceRetriever(Retriever):
    """把政策检索委托给受控业务数据源（tenant 作用域 + 完全自托管）。"""

    def __init__(self, data_source: BusinessDataSource, top_k: int = 4) -> None:
        self._ds = data_source
        self._top_k = top_k

    def search(self, tenant_id: str, query: str, top_k: int | None = None) -> list[dict]:
        return self._ds.search_policy(tenant_id, query, top_k or self._top_k)


def build_retriever(settings: Settings,
                    data_source: BusinessDataSource | None = None) -> Retriever:
    """按配置构造检索组件。默认：委托给受控业务数据源的政策检索（租户隔离、自托管）。

    `retrieval_backend` 差异仍保留：默认 keyword 返回把政策检索委托给数据源的
    `DataSourceRetriever`（对 mock/postgres 均正确的租户隔离 + 自托管）；milvus 返回
    由数据源政策文档构建的 Milvus 组件。KeywordRetriever 作为独立组件导出（本地/测试）。
    """
    backend = (settings.retrieval_backend or "keyword").strip().lower()
    if data_source is None:
        data_source = build_data_source(settings)

    if backend == "milvus":
        # 保留 Milvus 向量检索作为可选组件；以数据源为语料来源（租户隔离）。
        return MilvusRetriever(
            uri=settings.milvus_uri,
            db_name=settings.milvus_db_name,
            collection=settings.milvus_collection,
            top_k=settings.retrieval_top_k,
            dim=settings.milvus_embedding_dim,
            source_docs=data_source.policy_documents(),
        )
    if backend == "keyword":
        # 主图默认：把政策检索委托给受控业务数据源（mock/postgres 均租户隔离 + 自托管）。
        return DataSourceRetriever(data_source, top_k=settings.retrieval_top_k)
    raise ValueError(f"未知检索后端: {backend!r}（可选 keyword | milvus）")


__all__ = [
    "build_retriever", "Retriever", "KeywordRetriever", "MilvusRetriever",
    "DataSourceRetriever",
]
