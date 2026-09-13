"""真实 Milvus Lite 租户隔离与重复初始化验收。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from src.retrieval.milvus import MilvusRetriever


pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("milvus_lite") is None,
    reason="milvus-lite 未安装",
)


def test_real_milvus_lite_is_tenant_scoped_and_restart_safe(tmp_path: Path):
    uri = str(tmp_path / "policy.db")
    docs = [
        {"tenant_id": "TENANT-A", "doc_id": "a-1", "title": "退货政策", "content": "租户A七天退货"},
        {"tenant_id": "TENANT-B", "doc_id": "b-1", "title": "退货政策", "content": "租户B三十天退货"},
    ]
    first = MilvusRetriever(uri, "after_sales", "policy", dim=8, source_docs=docs)
    try:
        assert all(row["tenant_id"] == "TENANT-A" for row in first.search("TENANT-A", "退货"))
        assert all(row["tenant_id"] == "TENANT-B" for row in first.search("TENANT-B", "退货"))
        assert first.search("", "退货") == []
        first.delete_documents("TENANT-A", ["a-1"])
        assert first.search("TENANT-A", "退货") == []
        with pytest.raises(Exception):
            first.delete_documents("", ["b-1"])
    finally:
        if hasattr(first._client, "close"):
            first._client.close()

    second = MilvusRetriever(uri, "after_sales", "policy", dim=8, source_docs=[])
    try:
        assert all(row["tenant_id"] == "TENANT-A" for row in second.search("TENANT-A", "退货"))
    finally:
        if hasattr(second._client, "close"):
            second._client.close()
