"""离线验证 Milvus + 本地 embedding + Agentic RAG 子图联通。"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.graph.rag import make_rag_graph
from src.llm.mock import MockLLM
from src.retrieval.milvus import MilvusRetriever


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--output", default="evidence/MILVUS_RAG_INTEGRATION.json")
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="milvus-rag-"))
    client = None
    try:
        retriever = MilvusRetriever(
            str(root / "policy.db"), "after_sales", "policy", dim=512,
            embedding_provider="local_sentence_transformer",
            embedding_model_path=args.model_path,
            source_docs=[
                {"tenant_id": "TENANT-A", "doc_id": "a-1", "title": "退货政策", "content": "租户A支持七天退货，商品需保持完好。"},
                {"tenant_id": "TENANT-B", "doc_id": "b-1", "title": "退货政策", "content": "租户B支持三十天退货，商品需保持完好。"},
            ],
        )
        client = retriever._client
        graph = make_rag_graph(MockLLM(), retriever=retriever)
        results = {}
        for tenant in ("TENANT-A", "TENANT-B"):
            out = graph.invoke({
                "messages": [{"role": "user", "content": "退货政策"}],
                "tenant_id": tenant,
            })
            docs = out.get("retrieved_docs") or []
            assert docs and all(row["tenant_id"] == tenant for row in docs)
            assert out.get("falls_to_error") is False
            results[tenant] = {
                "doc_ids": [row["doc_id"] for row in docs],
                "rag_answer": out.get("rag_answer"),
                "hallucination_check": out.get("hallucination_check"),
            }
        payload = {"embedding_dim": 512, "backend": "milvus_lite", "results": results,
                   "tenant_scope_verified": True}
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False))
        print("MILVUS_RAG_INTEGRATION_OK")
        return 0
    finally:
        if client is not None and hasattr(client, "close"):
            client.close()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
