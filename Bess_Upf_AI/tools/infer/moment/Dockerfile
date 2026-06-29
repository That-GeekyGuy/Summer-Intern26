FROM python:3.11-slim

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
# momentfm is optional — sidecar degrades gracefully if unavailable
RUN pip install --no-cache-dir --timeout 300 \
    fastapi==0.111.0 \
    uvicorn[standard]==0.29.0 \
    torch==2.3.0+cpu \
    torchvision==0.18.0+cpu \
    --extra-index-url https://download.pytorch.org/whl/cpu \
    numpy==1.26.4

# Install momentfm separately (may not be on PyPI — skip if unavailable)
RUN pip install --no-cache-dir momentfm==0.1.1 || echo "momentfm not available — sidecar will use statistical features"

COPY infer/moment_server.py .

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8083/health')"

ENV HOST=0.0.0.0
ENV PORT=8083
ENV MODELS_DIR=/models

EXPOSE 8083

CMD ["python", "moment_server.py"]
