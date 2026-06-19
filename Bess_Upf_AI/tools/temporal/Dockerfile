FROM python:3.11-slim

WORKDIR /app

# gcc/g++ needed by scipy wheel compilation on slim base
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV HOST=0.0.0.0
ENV PORT=8085
ENV STL_PERIOD=96
ENV STL_REFIT_INTERVAL_HOURS=24
ENV HOLIDAY_COUNTRY=IN
ENV MODELS_DIR=/models
ENV DATA_DIR=/data

EXPOSE 8085

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8085/health')"

CMD ["python", "stl_service.py"]
