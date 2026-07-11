FROM python:3.12-slim

RUN addgroup --system ml && adduser --system --ingroup ml --no-create-home ml

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY train/ /app/train/
RUN chown -R ml:ml /app/train/data

USER ml

# Override CMD to run a specific training/tooling script, e.g.:
#   docker run --rm -v ./models:/models ... bess-upf-tools python train/prepare_dataset.py
CMD ["python", "train/train.py"]
