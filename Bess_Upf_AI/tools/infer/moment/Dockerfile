FROM nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-pip \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install PyTorch with CUDA 12.1 support (RTX 4060 = SM8.9, fully supported)
RUN pip3 install --no-cache-dir --timeout 300 \
    torch==2.3.0+cu121 \
    torchvision==0.18.0+cu121 \
    --extra-index-url https://download.pytorch.org/whl/cu121

# Install other Python dependencies
RUN pip3 install --no-cache-dir --timeout 300 \
    fastapi==0.111.0 \
    uvicorn[standard]==0.29.0 \
    numpy==1.26.4

# Install momentfm separately (may not be on PyPI — skip if unavailable)
RUN pip3 install --no-cache-dir momentfm==0.1.1 || echo "momentfm not available — sidecar will use statistical features"

COPY infer/moment_server.py .

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8083/health')"

ENV HOST=0.0.0.0
ENV PORT=8083
ENV MODELS_DIR=/models

EXPOSE 8083

CMD ["python3", "moment_server.py"]
