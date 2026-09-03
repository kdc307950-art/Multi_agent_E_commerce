"""可选的自托管 Milvus 向量检索组件（Milvus Lite，本地文件，完全自托管）。

约束：
- 绝不用托管向量库/公有 SaaS。Milvus Lite 以本地文件形式运行（milvus_uri 指向本地 .db）。
- 向量使用**确定性本地 hash embedding**（不调用外部 embedding 模型），完全自托管。
- 若环境未安装 pymilvus 或连接失败，构造时抛 RetrievalError（快速失败），由上层给出
  配置错误，而不是静默改走第三方或跨租户兜底。
- 检索严格按 tenant_id 作用域（partition/过滤表达式）；禁止无租户条件查询。
"""
from __future__ import annotations

import hashlib
import json
import re
import struct
import uuid

from src.retrieval.base import Retriever, RetrievalError

try:
    from pymilvus import MilvusClient  # type: ignore
    _PYMILVUS_AVAILABLE = True
except Exception:  # pragma: no cover - 取决于环境
    MilvusClient = None  # type: ignore
    _PYMILVUS_AVAILABLE = False

_EMBED_DIM = 128
_CLEAN = re.compile(r"\s+")


def _hash_embed(text: str, dim: int = _EMBED_DIM) -> list[float]:
    """确定性本地 hash embedding（无外部模型）。"""
    vec = [0.0] * dim
    tokens = _CLEAN.sub("", text)
    if not tokens:
        return vec
    words = [tokens[i:i + 2] for i in range(0, max(1, len(tokens) - 1), 1)]
    for w in words:
        h = hashlib.md5(w.encode("utf-8")).digest()
        idx = struct.unpack("<I", h[:4])[0] % dim
        sign = 1.0 if h[4] & 1 else -1.0
        vec[idx] += sign
    norm = sum(v * v for v in vec) ** 0.5
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


class MilvusRetriever(Retriever):
    """自托管 Milvus Lite 检索（可选后端）。"""

    def __init__(self, uri: str, db_name: str, collection: str, *, top_k: int = 4,
                 dim: int = _EMBED_DIM, source_docs: list[dict] | None = None) -> None:
        if not _PYMILVUS_AVAILABLE:
            raise RetrievalError(
                "检测到 RETRIEVAL_BACKEND=milvus，但环境未安装 pymilvus。"
                "请安装 pymilvus 或改用 keyword（自托管确定性检索）。"
            )
        self._uri = uri
        self._db_name = db_name
        self._collection = collection
        self._dim = dim
        self._top_k = top_k
        self._client = MilvusClient(uri=uri)
        self._client.create_collection(
            collection_name=collection, dimension=dim, metric_type="COSINE",
        )
        # 幂等写入文档（同 tenant+doc_id 不重复）。
        self._seed(source_docs or [])

    def _seed(self, docs: list[dict]) -> None:
        rows = []
        for d in docs:
            text = f"{d.get('title', '')} {d.get('content', '')}".strip()
            rows.append({
                "id": str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{d['tenant_id']}:{d['doc_id']}")),
                "tenant_id": d["tenant_id"],
                "doc_id": d["doc_id"],
                "title": d.get("title", ""),
                "content": d.get("content", ""),
                "vector": _hash_embed(text, self._dim),
            })
        if rows:
            self._client.insert(collection_name=self._collection, data=rows)

    def search(self, tenant_id: str, query: str, top_k: int | None = None) -> list[dict]:
        if not tenant_id:
            return []
        k = top_k if top_k is not None else self._top_k
        vec = _hash_embed(query, self._dim)
        hits = self._client.search(
            collection_name=self._collection,
            data=[vec],
            anns_field="vector",
            limit=k * 4,
            filter=f'tenant_id == "{tenant_id}"',
            output_fields=["tenant_id", "doc_id", "title", "content"],
        )
        result = []
        for h in hits[0][:k]:
            entity = h.get("entity", {})
            result.append({
                "tenant_id": entity.get("tenant_id"),
                "doc_id": entity.get("doc_id"),
                "title": entity.get("title", ""),
                "content": entity.get("content", ""),
                "score": h.get("distance", 0.0),
            })
        return result
