# Milvus + BGE 本地语义检索验收

日期：2026-09-13

## 环境

- Milvus：Milvus Lite，本地 `.db` 文件，完全自托管
- `pymilvus==3.0.1`
- `milvus-lite==3.2.1`
- embedding：ModelScope 缓存的 `BAAI/bge-small-zh-v1.5`
- 模型目录：`C:\Users\孔德草\.cache\modelscope\models\BAAI--bge-small-zh-v1.5\snapshots\master`
- embedding 维度：512

## 结果

- `encode("测试").shape == (1, 512)`：通过
- 归一化向量范数约为 `1.0`：通过
- `TENANT-A` 只召回 `TENANT-A` 文档：通过
- `TENANT-B` 只召回 `TENANT-B` 文档：通过
- 缺少 `tenant_id` 时返回空结果：通过
- 真实 Milvus Lite seed/search：通过

示例召回分数：

```text
TENANT-A / a1 / 0.7374
TENANT-B / b1 / 0.7078
```

## 边界

本报告证明本机 Milvus Lite + 本地 BGE 的语义检索和租户过滤，不证明 Milvus 集群高可用、生产备份恢复、跨节点性能或正式生产放量。生产仍保持 `NO-GO`。
