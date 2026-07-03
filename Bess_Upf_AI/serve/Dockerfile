FROM pytorch/pytorch:2.1.0-cuda11.8-cudnn8-runtime

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py feature_eng.py moment_loader.py shadow_publisher.py ./

ENV PYTHONPATH=/app
CMD ["serve", "run", "app:app_node", "--host", "0.0.0.0", "--port", "8000"]
