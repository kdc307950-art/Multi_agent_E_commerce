"""Milvus embedding provider 的边界测试。

这些测试不要求安装 pymilvus 或下载模型：默认 hash provider 必须零依赖，
本地模型 provider 的安装/目录/维度错误必须 fail-closed。
"""
from __future__ import annotations

import sys
import types

import pytest

from src.retrieval.base import RetrievalError
from src.retrieval.milvus import _build_embedder


def test_hash_provider_keeps_deterministic_zero_dependency_shape():
    embed = _build_embedder("hash", 8)
    first = embed("退货政策")
    second = embed("退货政策")
    assert len(first) == 8
    assert first == second


def test_unknown_provider_fails_closed():
    with pytest.raises(RetrievalError, match="未知 Milvus embedding provider"):
        _build_embedder("remote_api", 512)


def test_local_provider_requires_existing_local_directory(tmp_path):
    with pytest.raises(RetrievalError, match="模型目录不存在"):
        _build_embedder("local_sentence_transformer", 512, str(tmp_path / "missing"))


def test_local_provider_rejects_dimension_mismatch(monkeypatch, tmp_path):
    model_dir = tmp_path / "bge"
    model_dir.mkdir()

    class FakeModel:
        def __init__(self, path, *, local_files_only):
            assert path == str(model_dir)
            assert local_files_only is True

        def get_sentence_embedding_dimension(self):
            return 384

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        types.SimpleNamespace(SentenceTransformer=FakeModel),
    )
    with pytest.raises(RetrievalError, match="维度不匹配"):
        _build_embedder("local_sentence_transformer", 512, str(model_dir))


def test_local_provider_encodes_only_from_local_model(monkeypatch, tmp_path):
    model_dir = tmp_path / "bge"
    model_dir.mkdir()

    class FakeModel:
        def __init__(self, path, *, local_files_only):
            assert path == str(model_dir)
            assert local_files_only is True

        def get_sentence_embedding_dimension(self):
            return 3

        def encode(self, texts, *, normalize_embeddings, convert_to_numpy, show_progress_bar):
            assert texts == ["测试"]
            assert normalize_embeddings is True
            assert convert_to_numpy is True
            assert show_progress_bar is False
            return [[0.1, 0.2, 0.3]]

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        types.SimpleNamespace(SentenceTransformer=FakeModel),
    )
    embed = _build_embedder("local_sentence_transformer", 3, str(model_dir))
    assert embed("测试") == [0.1, 0.2, 0.3]
