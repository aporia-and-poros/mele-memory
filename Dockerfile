# Mele Memory：MCP 服务（HTTP）。bge-m3 不打进镜像，第一次启动下载到 /models（挂出来的目录），之后复用。
FROM python:3.12-slim

# WITH_EMBED=false 装一个不带向量模型的轻镜像（只能配 MELE_MEMORY_FAKE_EMBED=1 跑，测试用）
ARG WITH_EMBED=true

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models \
    MELE_MEMORY_WEB_DIR=/app/web

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY memory ./memory
COPY tide ./tide
COPY mcp_server ./mcp_server
COPY web ./web

# 先从 PyTorch 的 CPU 源装 torch：默认源的 Linux 版带 CUDA，镜像会大好几 GB
RUN if [ "$WITH_EMBED" = "true" ]; then \
      pip install torch --index-url https://download.pytorch.org/whl/cpu && \
      pip install ".[embed,mcp]"; \
    else \
      pip install ".[mcp]"; \
    fi

EXPOSE 8765
# 用 root 跑：/models 是挂进来的宿主目录，换成普通用户在 Linux 上常常写不进去
CMD ["python", "-m", "mcp_server", "--http", "--host", "0.0.0.0", "--port", "8765"]
