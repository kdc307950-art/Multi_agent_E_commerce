# 后端 API 镜像（Python 3.12）。Phase 1 默认内存存储；PostgreSQL 存储接入待实现/验证。
# 基础镜像走镜像加速器，并**钉到精确 digest**（@sha256:…），消除 `--pull` 可变 tag 漂移 → 镜像可复现。
FROM docker.1ms.run/library/python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

# SOURCE_DATE_EPOCH：固定源码/层内文件 mtime 与镜像 config `created` 时间戳（BuildKit 可复现构建），
# 使同 ref 冷构建（--no-cache）得到字节级一致的镜像。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STORAGE_BACKEND=memory \
    SOURCE_DATE_EPOCH=1704067200

WORKDIR /app

# 使用**精确锁定**的依赖（requirements-lock.txt 为 pip freeze 全量，含传递依赖），
# 避免 pip 在构建时重新解析导致依赖版本漂移。放大 pip 超时与重试（慢网络下下载完整 lock 集更稳）。
COPY requirements-lock.txt .
RUN pip install --no-cache-dir --timeout 120 --retries 5 -r requirements-lock.txt

COPY src ./src
COPY pyproject.toml .

EXPOSE 8000
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
