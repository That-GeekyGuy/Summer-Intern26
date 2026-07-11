FROM python:3.11-slim

WORKDIR /app

# CPU-only torch wheel: this service never touches a GPU (app.py loads
# ChronosPipeline with device_map="cpu"), but chronos-forecasting's default
# dependency resolution pulls the full CUDA-enabled torch build (~2GB+ of
# nvidia_cublas/cudnn/etc.) unless torch is satisfied first from the CPU index.
RUN pip install --no-cache-dir torch==2.1.0 --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./

ENV PYTHONPATH=/app
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8084"]
