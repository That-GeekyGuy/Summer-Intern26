FROM python:3.12-slim

RUN addgroup --system ml && adduser --system --ingroup ml --no-create-home ml

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# momentfm pins numpy==1.25.2 exactly, which has no prebuilt wheel for this
# image's Python 3.12 (forces an unsupported source build) — install its
# other deps normally, then momentfm itself with --no-deps so it uses the
# numpy>=1.26 already resolved above instead of its pinned version.
RUN pip install --no-cache-dir huggingface-hub transformers
RUN pip install --no-cache-dir --no-deps momentfm==0.1.4

COPY train/ /app/train/
RUN chown -R ml:ml /app/train/data

USER ml

# Override CMD to run a specific training/tooling script, e.g.:
#   docker run --rm -v ./models:/models ... bess-upf-tools python train/prepare_dataset.py
CMD ["python", "train/train.py"]
