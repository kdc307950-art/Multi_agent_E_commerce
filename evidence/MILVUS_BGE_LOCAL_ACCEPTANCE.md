# Milvus + BGE 本地语义检索验收

日期：2026-09-13

## 环境

- Milvus：Milvus Lite，本地 `.db` 文件，完全自托管
- `pymilvus==3.0.1`
- `milvus-lite==3.2.1`
- embedding：ModelScope 缓存的 `BAAI/bge-small-zh-v1.5`
- 模型目录：`C:\Users\孔德草\.cache\modelscope\models\BAAI--bge-small-zh-v1.5\snapshots\master`
- embedding 维度：512

模型文件 SHA-256（用于本机复现绑定）：

```text
config.json 3853a7979202c348751b753e36f579c41d8da7d36af617d3d907e1fc9b441f2a
model.safetensors 354763b9b1357bc9c44f62c6be2276321081ed2567773608c0d0785b61d5a026
modules.json 84e40c8e006c9b1d6c122e02cba9b02458120b5fb0c87b746c41e0207cf642cf
pytorch_model.bin 7c5fe667bbed05dc10e246e229b701ad266fe4d95ab946e9e5aa402056611b88
sentence_bert_config.json 84e39fda68ccbff05bfa723ae9c0e70e23e2ec373b76e0f8c6e71af72a693cbf
tokenizer_config.json e6f3b96db926a37d4039995fbf5ad17de158dfb8f6343d607e4dbaad18d75f5a
1_Pooling/config.json aaa8861589f80c961a03cc86c7eeaef7605c1676b9ab55329d33a304738769c6
```

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
