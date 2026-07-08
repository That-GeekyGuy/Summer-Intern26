FROM pytorch/pytorch:2.1.0-cuda11.8-cudnn8-runtime

# KubeRay's auto-generated head-pod readiness/liveness probes shell out to
# wget (confirmed via "bash: wget: command not found" probe failures on this
# base image) — it's not installed on the pytorch runtime image by default.
RUN apt-get update && apt-get install -y --no-install-recommends wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py feature_eng.py moment_loader.py shadow_publisher.py deploy_models.py ./

ENV PYTHONPATH=/app
CMD ["serve", "run", "app:app_node", "--host", "0.0.0.0", "--port", "8000"]
