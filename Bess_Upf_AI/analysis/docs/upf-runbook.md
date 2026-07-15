# CoreWatch Analysis Runbook

> **Stale sections warning**: this doc predates the V2 migration to ClickHouse-backed
> detection. There is no standalone `detection` Deployment/Service anymore — anomaly
> detection now runs inline in the Bytewax pipeline (statistical z-score + a call to
> `serve`'s `/detect_batch`, which runs Isolation Forest / Random Forest / MOMENT),
> writing to Kafka topic `upf.anomalies.critical`, consumed straight into ClickHouse's
> `anomaly_events` table via a Kafka-engine materialized view (see
> `charts/bess-upf/charts/clickhouse/files/init.sql`). The "Detection Service" and
> "Tier 1/2/3" sections below describe the old architecture and are kept for historical
> context only — commands referencing `deploy/detection` or `charts/.../detection/`
> will not work against the current cluster.

## Overview

This document covers operational procedures for the detection and LLM analysis services added in the second phase of the CoreWatch metrics stack.

---

## Detection Service

### What it does

Polls ClickHouse every 60 seconds (configurable via `POLL_INTERVAL`) and runs three rule types:
- **z-score**: flags spikes ≥ N standard deviations from the rolling window mean
- **trend**: flags sudden departures from the recent linear fit
- **threshold**: flags values outside static min/max bounds

Anomaly events are stored in ClickHouse `bess_upf.anomaly_events`.

### Checking recent anomalies

```bash
# Connect to the ClickHouse pod
kubectl exec -it -n bess-upf sts/clickhouse -- clickhouse-client

# With metric and severity filters
SELECT * FROM bess_upf.anomaly_events WHERE ts >= now() - INTERVAL 1 HOUR AND severity='high';
```

### Adding a new detection rule

1. Edit `charts/bess-upf/charts/detection/config/rules.yml`
2. Add a new entry under `metrics:` with the metric name, selector, and rule config
3. Upgrade the helm release or restart the detection service: `kubectl rollout restart deploy/detection -n bess-upf`

The rules file is mounted as a ConfigMap; changes take effect on restart.

### Adjusting thresholds

Z-score threshold (default 3.0): lower = more sensitive, higher = fewer false positives.
Trend deviation threshold (default 0.25): fraction of predicted value (0.25 = 25% deviation).

---

## LLM Analysis Service

### What it does

Provides a chat API at `/api/v1/chat`. Uses vLLM with explicit tool-calling:
- `query_clickhouse`: validated SQL queries against ClickHouse `upf_metrics` and `anomaly_events`
- `get_anomalies`: retrieves reactive (statistical) and ml (isolation forest / MOMENT) events directly from `bess_upf.anomaly_events`
- `get_metric_metadata`: lists allowed metric names

There is no `get_predictions` tool — the V2 pipeline has no predictive/forecast anomaly
event source. Forecast uncertainty bands come from a separate endpoint, `/api/v1/intervals`,
backed by the `chronos` service.

### Querying the chat API

```bash
# Via Ingress (external)
curl -s -k https://bess-upf.local/api/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"message": "What happened to UPF sessions in the last hour?"}'

# Continue a session
curl -s -k https://bess-upf.local/api/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"session_id": "<id from previous response>", "message": "Which UPF instance had the highest drop rate?"}'
```

### SQL validation failures

The validator rejects:
- Metric names not in the allowlist
- Time ranges > 30 days
- Selectors without an explicit `upf_id`

**Allowlist is empty at startup**: If ClickHouse is unreachable when `upf-analysis` starts, the allowlist stays empty and all `query_clickhouse` calls are rejected. Check ClickHouse health: `kubectl get pods -n bess-upf -l app=clickhouse`. The allowlist auto-repopulates within 5 minutes of ClickHouse recovery.

**Model uses unknown metric**: The validation error is returned as a tool result to the model, which should then call `get_metric_metadata` to discover valid names and retry.

### Viewing the query audit log

```bash
kubectl exec -it -n bess-upf sts/clickhouse -- clickhouse-client -q \
  "SELECT session_id, tool_name, execution_status, ts FROM bess_upf.action_audit ORDER BY ts DESC LIMIT 20;"
```

### vLLM health check

```bash
# From within the cluster
kubectl exec -it -n bess-upf deploy/analysis -- wget -qO- http://vllm:8000/health

# vLLM model info
kubectl exec -it -n bess-upf deploy/analysis -- wget -qO- http://vllm:8000/v1/models
```

### vLLM not starting

Common causes:
1. **VRAM insufficient**: Qwen3-8B with bitsandbytes INT4 needs ~5 GB VRAM. Check `nvidia-smi`.
2. **Model not cached**: On first start vLLM downloads the model. Check logs: `kubectl logs -n bess-upf deploy/vllm -f --tail=50`.
3. **Model name wrong**: Verify `model` in `vllm.yaml` values matches a valid Hugging Face model ID.

To use a different model size, update the helm values for vllm:
```yaml
# 8 GB VRAM — INT4 quantized (default)
vllm:
  model: "Qwen/Qwen2.5-7B-Instruct"
  quantization: "bitsandbytes"
  max_model_len: 2048

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
kubectl rollout restart deploy/detection -n bess-upf
kubectl rollout restart deploy/analysis -n bess-upf
# vLLM is stateless and can be restarted independently
kubectl rollout restart deploy/vllm -n bess-upf
```

The analysis service waits for ClickHouse to be healthy before starting, but does not wait for vLLM — the LLM client will fail individual requests until vLLM is ready.

---

## Tier 2 ML Anomaly Detection

Tier 2 adds a multivariate ML layer on top of Tier 1 (per-metric statistical rules) and Tier 3 (OLS forecast). It uses an Isolation Forest and a Random Forest classifier, served by Ray Serve.

### Architecture summary

```
Bytewax pipeline
  └─ Window operator (every 15 s)
       ├─ Assembles 39-feature vector (rates, ratios, rolling stats)
       ├─ Batches calls to Ray Serve (http://serve:8000/detect)
       └─ Emits event_type="ml" to Kafka if IF score > threshold AND RF prob > 0.5
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
# 1. Build the dataset
python tools/train/prepare_dataset.py

# 2. Train IF + RF
python tools/train/train_moment.py

# 3. Reload the models
python serve/deploy_models.py
```

### Checking Ray Serve health

```bash
kubectl exec -it -n bess-upf deploy/analysis -- wget -qO- http://serve:8000/health
# {"status":"ok","sklearn":true,"moment":true}
```

`models_loaded: false` means training artifacts are missing — run the training steps above.

### When Tier 2 fires but Tier 1 and Tier 3 do not

See the full decision tree in the main RUNBOOK (§13.3). Short version:
1. Check the "Top features" bar in the anomaly feed — if `ratio_dropped_per_session` or similar is dominant, the deviation is real but per-metric thresholds haven't been crossed yet.
2. If `training_report.md` is more than 30 days old, retrain before acting on the event.
3. Multiple rapid ML events with no Tier 1 activity → model drift, retrain first.
