FROM python:3.12-slim

RUN addgroup --system ml && adduser --system --ingroup ml --no-create-home ml

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY train/ /app/train/
COPY infer/ /app/infer/

USER ml

# Default: run the inference sidecar.
# Override CMD to run training:
#   docker run --rm -v ./models:/models ... bess-ml-tools python train/prepare_dataset.py
CMD ["python", "infer/server.py"]
