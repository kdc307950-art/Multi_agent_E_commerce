# 后端 API 镜像（Python 3.12）。Phase 1 默认内存存储；PostgreSQL 存储接入待实现/验证。
# 基础镜像走镜像加速器（本机对 docker.io 直连受限）。
FROM docker.1ms.run/library/python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STORAGE_BACKEND=memory

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY pyproject.toml .

EXPOSE 8000
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
