# RUNBOOK — BESS-UPF Metrics Pipeline

Quick reference for on-call and ops. Each section covers a failure mode, how to confirm it, and how to fix it.

---

## 1. remote_write is failing

### Symptoms
- Prometheus UI → Status → Runtime & Build Information shows `remote_storage` errors.
- `prometheus_remote_storage_failed_samples_total` is increasing.
- Metric data stops appearing in VictoriaMetrics.

### Confirm

```bash
# Check Prometheus logs for remote_write errors
docker compose logs prometheus | grep -i "remote_write\|failed\|error" | tail -30

# Check the remote_write queue depth
docker compose exec prometheus wget -qO- http://localhost:9090/api/v1/query \
  --post-data='query=prometheus_remote_storage_queue_highest_sent_timestamp_seconds'
```

### Causes and fixes

**A. VictoriaMetrics is down or unhealthy**
```bash
docker compose ps victoriametrics
docker compose logs victoriametrics | tail -50
docker compose restart victoriametrics
```

**B. Auth credentials mismatch** — VM returns 401, Prometheus logs show `server returned HTTP status 401 Unauthorized`.
```bash
# Verify the credentials in .env match VM's -httpAuth.* flags
docker compose exec victoriametrics cat /proc/1/cmdline | tr '\0' '\n' | grep httpAuth
# If mismatched, fix .env and restart both services:
docker compose up -d victoriametrics prometheus
```

**C. Queue full** — `prometheus_remote_storage_shards` is at `max_shards` and queue depth is at `capacity`.
```bash
# Increase max_shards in prometheus.yml → queue_config → max_shards
# Then hot-reload Prometheus (no restart needed):
docker compose exec prometheus wget -qO- --post-data='' http://localhost:9090/-/reload
```

**D. VictoriaMetrics disk full** — see section 3.

---

## 2. Export job failing

### Symptoms
- `docker compose logs export-job` shows a non-zero exit.
- No new Parquet files appear in MinIO after 02:00 UTC.
- Missing manifest markers: `_manifests/{metric}/date={date}.done` absent in MinIO.

### Confirm

```bash
docker compose logs export-job | tail -100
# Look for: "level":"ERROR" entries and the specific "err" field.

# Check if MinIO is reachable from the export-job container
docker compose exec export-job wget -qO- http://minio:9000/minio/health/live
```

### Causes and fixes

**A. VictoriaMetrics unreachable or auth failure**
- Check VM health (section 1 above).
- Confirm `VM_AUTH_USERNAME`/`VM_AUTH_PASSWORD` in `.env` match VM flags.

**B. MinIO unreachable**
```bash
docker compose ps minio
docker compose logs minio | tail -30
docker compose restart minio
```

**C. MinIO credentials wrong** — logs show `Access Denied`.
```bash
# Re-run the bootstrap to recreate the service account
docker compose run --rm minio-init
docker compose restart export-job
```

**D. Metric not in VictoriaMetrics** — logs show `no data in window, skipping`.
```bash
# Verify the metric name is correct
curl -sk -u admin:PASSWORD https://localhost/vm/api/v1/label/__name__/values | \
  python3 -m json.tool | grep upf_
# If missing, the scrape job isn't working — check section below on scrape failures.
```

**E. Force re-export for a specific date** (delete the manifest marker):
```bash
docker compose exec minio-init \
  mc rm local/upf-metrics/_manifests/upf_pdu_sessions_total/date=2024-01-15.done
# Then trigger an immediate run:
docker compose exec export-job /usr/local/bin/upf-exporter
```

---

## 3. VictoriaMetrics disk filling up

### Symptoms
- `vm_free_disk_space_bytes` metric is low.
- VM logs show `cannot create a metaindex file` or `not enough free disk space`.
- VM stops accepting remote_write (returns 503).

### Confirm

```bash
docker compose exec victoriametrics df -h /victoria-metrics-data
docker compose logs victoriametrics | grep -i "disk\|space\|error" | tail -20
```

### Fixes

**A. Immediate relief — remove old data beyond retention**
```bash
# VictoriaMetrics deletes data older than VM_RETENTION months automatically.
# If disk is full NOW, force a data cleanup:
curl -X POST http://victoriametrics:8428/internal/force_flush
```

**B. Expand the Docker volume**
```bash
# On Linux with an ext4 volume driver you can resize the underlying block device,
# then restart. On Docker Desktop, increase disk image size in settings.
docker compose stop victoriametrics
# Resize volume...
docker compose start victoriametrics
```

**C. Reduce retention** — lower `VM_RETENTION` in `.env` and restart VM. Data older than the new retention is purged within minutes.

**D. Check for cardinality explosion** — one bad scrape target can create millions of unique time series.
```bash
curl -sk -u admin:PASSWORD \
  "https://localhost/vm/api/v1/query?query=vm_new_timeseries_created_total[5m]"
```
If this is high, identify the offending metric in Prometheus status page and add a `metric_relabel_configs` drop rule or fix the target's label set.

---

## 4. Prometheus scrape failures

### Symptoms
- `up` metric is 0 for some UPF targets.
- `prometheus_target_scrape_pool_exceeded_target_limit` increasing.

### Confirm

```bash
# Internal access to Prometheus targets page
docker compose exec prometheus wget -qO- http://localhost:9090/api/v1/targets | \
  python3 -m json.tool | grep -A3 '"health":"down"'
```

### Fixes

**A. Target unreachable** — network path from Prometheus to UPF host is broken.
```bash
# Prometheus is on scrape-net which bridges to the host network
docker compose exec prometheus ping -c 3 10.0.1.10
# If that fails, check Docker scrape-net config and host routing
```

**B. Wrong port or path** — edit `config/prometheus/targets/upf-targets.yml`.
Prometheus hot-reloads within 30 s (no restart needed).

**C. TLS mismatch on UPF endpoint** — add `tls_config: {insecure_skip_verify: true}` to the job in `prometheus.yml` (dev only) or provide the CA cert.

---

## 5. Caddy / TLS issues

### Symptoms
- Browser shows TLS error or `ERR_CERT_AUTHORITY_INVALID`.
- Caddy fails to start (port 443 already in use or cert file missing).

### Fixes

**A. Dev cert expired or missing**
```bash
bash scripts/gen-dev-certs.sh
docker compose restart caddy
```

**B. Port 443 conflict**
```bash
# Find the process holding 443 on the host
netstat -tlnp | grep 443
```

**C. Production ACME failing** — Caddy can't reach Let's Encrypt.
```bash
docker compose logs caddy | grep -i "acme\|cert\|error"
# Ensure port 80 is publicly reachable for HTTP-01 challenge,
# or switch to DNS-01 with a Caddy DNS provider plugin.
```

---

## 6. Service restart order

If you need to bring everything down and back up cleanly:

```bash
docker compose down
docker compose up -d minio
docker compose up -d minio-init   # wait for it to exit 0
docker compose up -d victoriametrics prometheus export-job caddy
docker compose ps                  # confirm all healthy
```

## 7. Useful one-liners

```bash
# Tail all service logs together
docker compose logs -f --tail=50

# Check remote_write success rate
docker compose exec prometheus wget -qO- \
  'http://localhost:9090/api/v1/query?query=rate(prometheus_remote_storage_succeeded_samples_total[5m])'

# List exported Parquet partitions for a metric
docker compose exec minio-init \
  mc ls --recursive local/upf-metrics/upf_pdu_sessions_total/

# Query VictoriaMetrics directly (bypasses Caddy, internal access)
docker compose exec prometheus \
  wget -qO- 'http://victoriametrics:8428/api/v1/query?query=upf_pdu_sessions_total&time=now' \
  --header='Authorization: Basic <base64(user:pass)>'

# Hot-reload Prometheus config after editing prometheus.yml or targets/*.yml
docker compose exec prometheus wget -qO- --post-data='' http://localhost:9090/-/reload
```

---

## 8. Detection service not firing anomalies

### Symptoms
- `GET /anomalies` returns empty array when anomalies are expected.
- `docker compose logs detection` shows errors or no "detection pass complete" lines.

### Confirm

```bash
# Check service health
docker compose exec detection wget -qO- http://localhost:8081/health

# Check for recent anomalies (last hour)
docker compose exec detection \
  wget -qO- "http://localhost:8081/anomalies?since=$(date -d '1 hour ago' +%s)"

# Check that rules.yml is mounted and parseable
docker compose exec detection cat /etc/detection/rules.yml
```

### Causes and fixes

**A. VictoriaMetrics unreachable**
```bash
docker compose exec detection \
  wget -qO- 'http://victoriametrics:8428/health'
```
Fix: restart `victoriametrics`, then `detection`.

**B. Metrics not in VM** — query returns no series, so no rules fire.
```bash
curl -sk -u admin:PASSWORD \
  "https://localhost/vm/api/v1/label/__name__/values" | \
  python3 -m json.tool | grep upf_
```
If missing, check Prometheus scrape health (section 4).

**C. Thresholds too wide** — lower the threshold in `rules.yml` and restart:
```bash
docker compose restart detection
```

**D. Rules file parse error** — detection logs will show a YAML parse error on startup.
```bash
docker compose logs detection | grep -i "error\|yaml\|parse" | head -20
```

---

## 9. LLM analysis service failures

### Symptoms
- `POST /api/v1/chat` returns 5xx or connection refused through Caddy.
- Analysis container exits or is in restart loop.

### Confirm

```bash
curl -k https://localhost/api/v1/health
# Expected: 200 OK  {"status":"ok"}

docker compose ps analysis vllm
docker compose logs analysis | tail -50
```

### Causes and fixes

**A. vLLM not ready** — model loads in ~60 s on first start.
```bash
docker compose logs vllm | tail -30
# Wait for "Application startup complete"
docker compose exec vllm nvidia-smi
```

**B. HuggingFace token missing / wrong** — vLLM fails to download the model.
```bash
docker compose logs vllm | grep -i "token\|auth\|error" | head -20
# Fix HF_TOKEN in .env, then:
docker compose up -d vllm
```

**C. Allowlist empty at startup** — all PromQL queries are denied.
```bash
docker compose logs analysis | grep -i "allowlist\|refresh" | head -20
# Fix: ensure victoriametrics is healthy, then:
docker compose restart analysis
```

**D. GPU out of memory**
```bash
docker compose logs vllm | grep -i "cuda\|oom\|killed"
# Lower --gpu-memory-utilization in docker-compose.yml vllm.command, then:
docker compose up -d vllm
```

---

## 10. PromQL validation failures

Validation errors are returned as tool results (not fatal) so the model can reformulate. They are also written to the audit log.

| Error | Cause | Action |
|---|---|---|
| `metric "X" not in allowlist` | Metric absent from VM or VM unreachable | Verify metric name; check `/vm/api/v1/label/__name__/values` |
| `selector without metric name` | Query like `{job="upf"}` | Always include a metric name |
| `time_range exceeds maximum` | `time_range` > `MAX_QUERY_RANGE` (default 30 d) | Reduce the time range |
| `step too small` | `step` < `MIN_STEP` (default 15 s) | Use a larger step |
| `parse error` | Malformed PromQL from model | Check vLLM health; model should auto-retry |

**Check allowlist size:**
```bash
docker compose logs analysis | grep "allowlist" | tail -5
# Shows "allowlist refreshed" with count field
```

**Force allowlist refresh** — restart the analysis service (runs 3 attempts on startup).

---

## 11. Querying the audit log

```bash
docker compose exec analysis sqlite3 /data/audit.db
```

```sql
-- Recent tool calls (last hour)
SELECT created_at, session_id, tool_name, validation_error, execution_status
FROM query_audit
WHERE created_at > datetime('now', '-1 hour')
ORDER BY created_at DESC LIMIT 50;

-- All validation failures
SELECT created_at, session_id, tool_args, validation_error
FROM query_audit WHERE validation_error != ''
ORDER BY created_at DESC LIMIT 20;

-- Queries per session today
SELECT session_id, COUNT(*) AS queries
FROM query_audit WHERE created_at > date('now')
GROUP BY session_id ORDER BY queries DESC;
```

**Export for compliance:**
```bash
docker compose exec analysis \
  sqlite3 -csv /data/audit.db \
  "SELECT * FROM query_audit WHERE created_at > date('now', '-30 days');" \
  > audit-last-30d.csv
```

---

## 12. vLLM health check

```bash
# OpenAI-compatible health endpoint (from within metrics-backend)
docker compose exec analysis wget -qO- http://vllm:8000/health

# List loaded models
docker compose exec analysis wget -qO- http://vllm:8000/v1/models

# GPU utilization
docker compose exec vllm nvidia-smi \
  --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader
```

---

## 13. Tier 2 ML anomaly detection

Tier 2 evaluates all UPF metrics **jointly** (multivariate) using an Isolation Forest (unsupervised) and a Random Forest classifier (supervised). It runs inside the `detection` service and calls the `ml-infer` sidecar for scoring. Events appear in the feed with type `ml`.

### 13.1 Sidecar health

```bash
# Health endpoint — returns {"status":"ok"} when models are loaded
docker compose exec detection wget -qO- http://ml-infer:8080/health

# From outside (if the port is exposed)
curl -s http://localhost:8080/health
```

Expected response when models are loaded:
```json
{"status": "ok", "models_loaded": true}
```

If `models_loaded` is `false` or the request times out, see §13.3.

### 13.2 Check whether models have been trained

```bash
# List model artifacts inside the models volume
docker compose run --rm --entrypoint ls \
  -v models-data:/models ml-infer /models

# Expected files after a successful training run:
#   isolation_forest.joblib
#   random_forest.joblib
#   scaler.joblib
#   scaler_params.json
#   metadata.json
#   feature_columns.json
#   dataset.parquet
#   training_report.md
```

If any of the `.joblib` / `.json` files are missing, the models have not been trained yet (or training failed). Run §13.4 to train.

### 13.3 Tier 2 fires but Tier 1 and Tier 3 do not

This is the most common diagnostic scenario. Two root causes are likely:

**A. Genuine multivariate anomaly**

Tier 1 checks each metric independently (z-score, threshold). Tier 2 detects *correlated* deviations that are each individually within normal bounds but collectively anomalous — for example, a slow session-count increase combined with a disproportionate packet-drop rate.

Steps to confirm:
1. Open the event in the anomaly feed and expand the "Top features" bar chart.
2. Note which features dominate (e.g., `rate_port_dropped_count`, `ratio_dropped_per_session`).
3. Pull recent values in the PromQL sandbox or Grafana:
   ```promql
   rate(port_dropped_count[5m]) / rate(pfcp_sessions_total[5m])
   ```
4. If the ratio is elevated compared to the past 24 h but still below the Tier 1 threshold, this is a genuine multivariate signal. Consider lowering the Tier 1 threshold or filing a capacity review.

**B. Model drift**

If the UPF workload has changed significantly since training (new scenarios, hardware upgrade, sustained load change), the model's idea of "normal" may be stale.

Steps to distinguish drift from a genuine anomaly:
1. Check `training_report.md` for the training date and IF/RF metrics:
   ```bash
   docker compose run --rm --entrypoint cat \
     -v models-data:/models ml-infer /models/training_report.md
   ```
2. If the training date is more than 30 days ago, or the RF validation F1 is below 0.80, schedule a retrain (§13.4).
3. Examine the IF score in the event: an IF score barely above the threshold (< 0.1 above) with an RF probability near 0.50 suggests a borderline or noisy signal — treat as low-confidence.
4. If multiple Tier 2 events fire in rapid succession on otherwise-quiet metrics, that is a strong model-drift signal. Retrain before acting on the anomalies.

**Quick decision tree:**

```
Tier 2 fires alone
├─ Top features show a known-bad ratio (dropped/session spikes)?
│   └─ YES → Genuine multivariate anomaly. Review capacity.
├─ Training report > 30 days old OR RF F1 < 0.80?
│   └─ YES → Retrain (§13.4), then re-evaluate.
├─ Multiple ML events in < 10 min, no Tier 1 events at all?
│   └─ YES → Likely model drift. Retrain before acting.
└─ None of the above → Treat as low-confidence. Monitor for 15 min.
    If it repeats, investigate the top feature trends manually.
```

### 13.4 Retrain the models

Training reads Parquet files from MinIO, so the data export job must have run at least once and `upf_sim_scenario` must be in `EXPORT_METRICS` (see `.env`).

```bash
# Run prepare_dataset.py — downloads Parquet, engineers features, writes dataset.parquet
docker compose run --rm \
  -v models-data:/models \
  --env-file .env \
  ml-infer \
  python train/prepare_dataset.py \
    --bucket "$EXPORT_BUCKET" \
    --endpoint-url "http://minio:9000"

# Run train.py — fits IF + RF, writes all model artifacts
docker compose run --rm \
  -v models-data:/models \
  ml-infer \
  python train/train.py

# Restart the sidecar so it reloads the new models
docker compose restart ml-infer

# Confirm new models are loaded
docker compose exec detection wget -qO- http://ml-infer:8080/health
```

Training typically takes 1–3 minutes depending on dataset size. Check `training_report.md` afterwards (see §13.3 step 1) to validate RF F1 and IF threshold.

### 13.5 Disable Tier 2 temporarily

If the models are producing excessive false positives during an incident, disable Tier 2 without restarting the detection service:

```bash
# Stop the sidecar — detection will degrade gracefully to Tier 1 + Tier 3 only
docker compose stop ml-infer
```

The detection service polls `http://ml-infer:8080/health` and skips ML evaluation when the sidecar is unavailable. No restart needed.

To re-enable:
```bash
docker compose start ml-infer
```

### 13.6 Inspect raw ML scores

The `ml-infer` sidecar exposes a `/predict` endpoint you can query directly for debugging:

```bash
# Example: score a hand-crafted feature vector (adjust values to match recent metrics)
docker compose exec detection \
  wget -qO- --post-data='{"features":{"rate_pfcp_sessions":12000,"rate_port_bytes_count":1200000,"rate_port_packets_count":800000,"rate_port_dropped_count":0,"ratio_dropped_per_session":0,"ratio_bytes_per_session":100,"ratio_packets_per_session":66,"roll_mean_rate_pfcp_sessions":12000,"roll_std_rate_pfcp_sessions":50,"roll_min_rate_pfcp_sessions":11900,"roll_max_rate_pfcp_sessions":12100,"roll_mean_rate_port_bytes_count":1200000,"roll_std_rate_port_bytes_count":5000,"roll_min_rate_port_bytes_count":1195000,"roll_max_rate_port_bytes_count":1205000,"roll_mean_rate_port_packets_count":800000,"roll_std_rate_port_packets_count":3000,"roll_min_rate_port_packets_count":797000,"roll_max_rate_port_packets_count":803000,"roll_mean_rate_port_dropped_count":0,"roll_std_rate_port_dropped_count":0,"roll_min_rate_port_dropped_count":0,"roll_max_rate_port_dropped_count":0,"roll_mean_ratio_dropped_per_session":0,"roll_std_ratio_dropped_per_session":0,"roll_min_ratio_dropped_per_session":0,"roll_max_ratio_dropped_per_session":0,"roll_mean_ratio_bytes_per_session":100,"roll_std_ratio_bytes_per_session":0.5,"roll_min_ratio_bytes_per_session":99,"roll_max_ratio_bytes_per_session":101,"roll_mean_ratio_packets_per_session":66,"roll_std_ratio_packets_per_session":0.3,"roll_min_ratio_packets_per_session":65,"roll_max_ratio_packets_per_session":67}}' \
  --header='Content-Type:application/json' \
  http://ml-infer:8080/predict
```

Response fields:
- `if_score` — Isolation Forest anomaly score; positive = anomalous
- `rf_score` — Random Forest anomaly probability (0–1)
- `rf_class` — 0 (normal) or 1 (anomaly)
- `feature_contributions` — top-3 RF feature importances
- `available` — false if models are not loaded
