FROM python:3.10-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./pipeline/
RUN chmod +x pipeline/start.sh

ENV PYTHONPATH=/app
CMD ["./pipeline/start.sh"]
