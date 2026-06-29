FROM python:3.11-slim

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
# chronos is installed from PyPI; it bundles chronos-t5-small weights via HuggingFace
RUN pip install --no-cache-dir --timeout 300 \
    fastapi==0.111.0 \
    uvicorn[standard]==0.29.0 \
    torch==2.3.0+cpu \
    --extra-index-url https://download.pytorch.org/whl/cpu \
    numpy==1.26.4 \
    pandas==2.2.2

# Install chronos separately (degrades to naive baseline if unavailable)
RUN pip install --no-cache-dir "chronos-forecasting[training]==1.4.0" \
    || pip install --no-cache-dir "git+https://github.com/amazon-science/chronos-forecasting.git@main" \
    || echo "chronos not available — sidecar will use naive baseline"

COPY infer/chronos_server.py .

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8084/health')"

ENV HOST=0.0.0.0
ENV PORT=8084
ENV MODELS_DIR=/models

EXPOSE 8084

CMD ["python", "chronos_server.py"]
