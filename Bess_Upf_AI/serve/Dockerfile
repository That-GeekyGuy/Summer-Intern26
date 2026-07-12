FROM pytorch/pytorch:2.1.0-cuda11.8-cudnn8-runtime

# KubeRay's auto-generated head-pod readiness/liveness probes shell out to
# wget (confirmed via "bash: wget: command not found" probe failures on this
# base image) — it's not installed on the pytorch runtime image by default.
RUN apt-get update && apt-get install -y --no-install-recommends wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# momentfm pins numpy==1.25.2 exactly, which would downgrade the numpy this
# image otherwise resolves and break joblib.load() for the sklearn models
# (pickled with a newer numpy — "No module named 'numpy._core'" on unpickle
# with older numpy). Install without its declared deps, same fix as
# tools/Dockerfile, so it uses the numpy already resolved above.
RUN pip install --no-cache-dir --no-deps momentfm==0.1.4

COPY app.py feature_eng.py moment_loader.py shadow_publisher.py deploy_models.py ./

ENV PYTHONPATH=/app
CMD ["serve", "run", "app:app_node", "--host", "0.0.0.0", "--port", "8000"]
