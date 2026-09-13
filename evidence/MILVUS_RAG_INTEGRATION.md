# Milvus → Agentic RAG 联通验收

日期：2026-09-13

执行：

```powershell
.\.venv\Scripts\python.exe scripts\verify_milvus_rag.py `
  --model-path C:\models\bge-small-zh-v1.5
```

结果：`MILVUS_RAG_INTEGRATION_OK`

- 本地 BGE 输出 512 维向量；
- Milvus Lite 按租户过滤政策文档；
- Agentic RAG 子图成功读取当前租户文档并生成回答；
- 两个租户的 `doc_id`、回答和来源均未串租户；
- Mock 幻觉校验通过；
- 结果写入 `evidence/MILVUS_RAG_INTEGRATION.json`。

该报告证明本机 Milvus Lite → Agentic RAG 的联通，不证明生产集群高可用、模型线上稳定性或真实租户业务准确率。
