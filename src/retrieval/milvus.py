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
import math
import re
import struct
import uuid
from pathlib import Path
from typing import Any, Callable

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


def _build_embedder(provider: str, dim: int, model_path: str = "") -> Callable[[str], list[float]]:
    """构造明确的本地 embedding provider。

    默认 hash provider 保持现有零依赖行为；真实模型必须显式配置本地目录，
    任何安装/加载/维度问题都抛出 RetrievalError，避免把失败伪装成 hash 检索。
    """
    normalized = (provider or "hash").strip().lower()
    if normalized == "hash":
        if dim <= 0:
            raise RetrievalError("Milvus embedding 维度必须为正整数")
        return lambda text: _hash_embed(text, dim)
    if normalized not in {"local_sentence_transformer", "sentence_transformer"}:
        raise RetrievalError(
            f"未知 Milvus embedding provider: {provider!r}（可选 hash | local_sentence_transformer）"
        )
    if not model_path:
        raise RetrievalError(
            "local_sentence_transformer 必须配置本地 milvus_embedding_model_path；"
            "不允许运行时下载模型"
        )
    model_dir = Path(model_path).expanduser()
    if not model_dir.exists() or not model_dir.is_dir():
        raise RetrievalError(f"本地 embedding 模型目录不存在: {model_dir}")
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except Exception as exc:  # pragma: no cover - 取决于可选环境
        raise RetrievalError(
            "已选择 local_sentence_transformer，但环境未安装 sentence-transformers"
        ) from exc
    try:
        # 只允许离线加载。不要为兼容旧版 API 回退到无 local_files_only 的
        # 构造方式，否则缺文件时可能隐式访问网络，违反完全自托管边界。
        model: Any = SentenceTransformer(str(model_dir), local_files_only=True)
        actual_dim = int(model.get_sentence_embedding_dimension())
    except Exception as exc:  # pragma: no cover - 取决于可选环境
        raise RetrievalError(f"本地 embedding 模型加载失败: {model_dir}") from exc
    if actual_dim != dim:
        raise RetrievalError(
            f"embedding 维度不匹配: 配置 dim={dim}，模型输出 dim={actual_dim}；"
            "请重建 Milvus collection 后再切换维度"
        )

    def embed(text: str) -> list[float]:
        try:
            values = model.encode(
                [text], normalize_embeddings=True, convert_to_numpy=True,
                show_progress_bar=False,
            )[0]
            result = [float(value) for value in values]
        except Exception as exc:  # pragma: no cover - 取决于可选运行时
            raise RetrievalError("本地 embedding 推理失败") from exc
        if len(result) != dim:
            raise RetrievalError(f"embedding 输出维度异常: 期望 {dim}，实际 {len(result)}")
        if not all(math.isfinite(value) for value in result):
            raise RetrievalError("embedding 输出包含 NaN/Inf，拒绝写入或检索")
        return result

    return embed


class MilvusRetriever(Retriever):
    """自托管 Milvus Lite 检索（可选后端）。"""

    def __init__(self, uri: str, db_name: str, collection: str, *, top_k: int = 4,
                 dim: int = _EMBED_DIM, embedding_provider: str = "hash",
                 embedding_model_path: str = "", source_docs: list[dict] | None = None) -> None:
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
        self._embed = _build_embedder(embedding_provider, dim, embedding_model_path)
        self._client = MilvusClient(uri=uri)
        self._client.create_collection(
            collection_name=collection, dimension=dim, metric_type="COSINE",
        )
        # 幂等写入文档（同 tenant+doc_id 不重复）。
        self._seed(source_docs or [])

    def _seed(self, docs: list[dict]) -> None:
        rows = []
        seen: set[str] = set()
        for d in docs:
            if not d.get("tenant_id") or not d.get("doc_id"):
                raise RetrievalError("Milvus seed 文档缺少 tenant_id/doc_id")
            row_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{d['tenant_id']}:{d['doc_id']}"))
            if row_id in seen:
                continue
            seen.add(row_id)
            text = f"{d.get('title', '')} {d.get('content', '')}".strip()
            rows.append({
                "id": row_id,
                "tenant_id": d["tenant_id"],
                "doc_id": d["doc_id"],
                "title": d.get("title", ""),
                "content": d.get("content", ""),
                "vector": self._embed(text),
            })
        if rows:
            # upsert makes repeated application startup/seed idempotent when supported
            # by Milvus Lite; retain insert fallback for older local clients.
            if hasattr(self._client, "upsert"):
                self._client.upsert(collection_name=self._collection, data=rows)
            else:
                self._client.insert(collection_name=self._collection, data=rows)

    def search(self, tenant_id: str, query: str, top_k: int | None = None) -> list[dict]:
        if not tenant_id:
            return []
        if any(ord(ch) < 32 for ch in tenant_id):
            return []
        k = top_k if top_k is not None else self._top_k
        vec = self._embed(query)
        hits = self._client.search(
            collection_name=self._collection,
            data=[vec],
            anns_field="vector",
            limit=k * 4,
            filter=f'tenant_id == {json.dumps(tenant_id, ensure_ascii=False)}',
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
