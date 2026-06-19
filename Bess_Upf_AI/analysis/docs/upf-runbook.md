# BESS-UPF Analysis Runbook

## Overview

This document covers operational procedures for the detection and LLM analysis services added in the second phase of the BESS-UPF metrics stack.

---

## Detection Service

### What it does

Polls VictoriaMetrics every 60 seconds (configurable via `POLL_INTERVAL`) and runs three rule types:
- **z-score**: flags spikes ≥ N standard deviations from the rolling window mean
- **trend**: flags sudden departures from the recent linear fit
- **threshold**: flags values outside static min/max bounds

Anomaly events are stored in SQLite at `/data/anomalies.db` and served at `http://detection:8081/anomalies`.

### Checking recent anomalies

```bash
# From the host
docker compose exec detection wget -qO- 'http://localhost:8081/anomalies?since=1748000000'

# With metric and severity filters
docker compose exec detection wget -qO- \
  'http://localhost:8081/anomalies?since=1748000000&metric=upf_pdu_sessions_total&severity=high'
```

### Adding a new detection rule

1. Edit `config/detection/rules.yml`
2. Add a new entry under `metrics:` with the metric name, selector, and rule config
3. Restart the detection service: `docker compose restart detection`

The rules file is mounted read-only; changes take effect on restart.

### Adjusting thresholds

Z-score threshold (default 3.0): lower = more sensitive, higher = fewer false positives.
Trend deviation threshold (default 0.25): fraction of predicted value (0.25 = 25% deviation).

---

## LLM Analysis Service

### What it does

Provides a chat API at `/api/v1/chat` (proxied through Caddy). Uses vLLM (Qwen3-8B) with explicit tool-calling:
- `query_prometheus`: validated PromQL range queries against VictoriaMetrics
- `get_anomalies`: retrieves events from the detection service
- `get_metric_metadata`: lists allowed metric names

### Querying the chat API

```bash
# Via Caddy (external)
curl -s -u "${CADDY_ADMIN_USER}:${CADDY_ADMIN_PASS}" \
  https://localhost/api/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"message": "What happened to UPF sessions in the last hour?"}'

# Continue a session
curl -s -u "${CADDY_ADMIN_USER}:${CADDY_ADMIN_PASS}" \
  https://localhost/api/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"session_id": "<id from previous response>", "message": "Which UPF instance had the highest drop rate?"}'
```

### PromQL validation failures

The validator rejects:
- Metric names not in the allowlist (fetched from VM at startup + every 5 min)
- Time ranges > 30 days
- Steps < 15 seconds
- Selectors without an explicit metric name (`{job="upf"}` alone)

**Allowlist is empty at startup**: If VM is unreachable when `upf-analysis` starts, the allowlist stays empty and all `query_prometheus` calls are rejected. Check VM health: `docker compose ps victoriametrics`. The allowlist auto-repopulates within 5 minutes of VM recovery.

**Model uses unknown metric**: The validation error is returned as a tool result to the model, which should then call `get_metric_metadata` to discover valid names and retry.

### Viewing the query audit log

```bash
docker compose exec analysis \
  sqlite3 /data/audit.db \
  "SELECT session_id, tool_name, validation_error, execution_status, datetime(created_at,'unixepoch') FROM query_audit ORDER BY created_at DESC LIMIT 20;"
```

### vLLM health check

```bash
# From within the metrics-backend network
docker compose exec analysis wget -qO- http://vllm:8000/health

# vLLM model info
docker compose exec analysis wget -qO- http://vllm:8000/v1/models
```

### vLLM not starting

Common causes:
1. **VRAM insufficient**: Qwen3-8B with bitsandbytes INT4 needs ~5 GB VRAM. Check `nvidia-smi`.
2. **Model not cached**: On first start vLLM downloads the model. Check logs: `docker compose logs vllm --tail=50 -f`.
3. **Model name wrong**: Verify `VLLM_MODEL` in `.env` matches a valid Hugging Face model ID.

To use a different model size:
```bash
# 8 GB VRAM — INT4 quantized (default)
VLLM_MODEL=Qwen/Qwen3-8B
VLLM_EXTRA_ARGS=--quantization bitsandbytes --max-model-len 2048

# 16-24 GB VRAM — FP16 full precision
VLLM_MODEL=Qwen/Qwen3-8B
VLLM_EXTRA_ARGS=--max-model-len 4096
```

### Rate limiting

The chat endpoint is rate-limited to 10 requests per minute per client IP (in-memory, resets on restart). The limit is configured in `analysis/main.go` and requires a code change to adjust for v1.

---

## Service Restart Order

When restarting the full detection+analysis stack:

```bash
docker compose restart detection
docker compose restart analysis
# vLLM is stateless and can be restarted independently
docker compose restart vllm
```

The analysis service waits for VM to be healthy (via the depends_on condition) before starting, but does not wait for vLLM — the LLM client will fail individual requests until vLLM is ready.

---

## Tier 2 ML Anomaly Detection

Tier 2 adds a multivariate ML layer on top of Tier 1 (per-metric statistical rules) and Tier 3 (OLS forecast). It uses an Isolation Forest and a Random Forest classifier, served by the `ml-infer` sidecar.

### Architecture summary

```
detection service (Go)
  └─ tier2.Evaluator (every 60 s)
       ├─ Fetches last 5 min of raw metrics from VictoriaMetrics
       ├─ Assembles 39-feature vector (rates, ratios, rolling stats)
       ├─ Calls ml-infer:8080/predict (5 s timeout, graceful degrade)
       └─ Emits event_type="ml" to SQLite if IF score > threshold AND RF prob > 0.5
```

Models must be trained before Tier 2 activates. Until then the detection service logs `tier2: models not available, skipping` and continues with Tier 1 + Tier 3.

### Tier 2 events in the LLM context

When the LLM receives a `get_anomalies` tool call, ML events appear with:
- `event_type: "ml"`
- `metric_name: "upf_multivariate"` — not a real Prometheus metric
- `feature_contributions` JSON: top-3 RF feature importances

The LLM is aware of these fields. A well-formed question like *"Why did the ML detector fire at 14:30?"* will trigger the LLM to inspect `feature_contributions` and the surrounding Tier 1 events in its response.

### Training the models (first-time setup)

```bash
# 1. Ensure Parquet data exists in MinIO (export job must have run at least once)
docker compose exec minio mc ls local/upf-metrics/

# 2. Build the dataset
docker compose run --rm \
  -v models-data:/models \
  --env-file .env \
  ml-infer \
  python train/prepare_dataset.py \
    --bucket "$EXPORT_BUCKET" \
    --endpoint-url "http://minio:9000"

# 3. Train IF + RF
docker compose run --rm \
  -v models-data:/models \
  ml-infer \
  python train/train.py

# 4. Reload the sidecar
docker compose restart ml-infer
```

### Checking sidecar health

```bash
docker compose exec detection wget -qO- http://ml-infer:8080/health
# {"status":"ok","models_loaded":true}
```

`models_loaded: false` means training artifacts are missing — run the training steps above.

### When Tier 2 fires but Tier 1 and Tier 3 do not

See the full decision tree in the main RUNBOOK (§13.3). Short version:
1. Check the "Top features" bar in the anomaly feed — if `ratio_dropped_per_session` or similar is dominant, the deviation is real but per-metric thresholds haven't been crossed yet.
2. If `training_report.md` is more than 30 days old, retrain before acting on the event.
3. Multiple rapid ML events with no Tier 1 activity → model drift, retrain first.
