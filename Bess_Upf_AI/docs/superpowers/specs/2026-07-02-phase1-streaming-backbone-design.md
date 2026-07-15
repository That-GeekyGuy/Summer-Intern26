# CoreWatch Phase 1 — Real Streaming Backbone Design

**Date:** 2026-07-02
**Status:** Approved (design), pending implementation plan
**Parent spec:** `docs/superpowers/specs/2026-07-01-bess-upf-v2-rebuild-design.md` (Phase 1 section)

---

## 1. Goal

Replace the v2 stub pipeline (deque loop, silent-zero scraper) with a real streaming backbone:
`upf-sim -> Redpanda -> (Bytewax windowing + ClickHouse sink)`.

**Phase 1 exit criterion:** `SELECT count(*), now() - max(ts) AS lag FROM upf_metrics` returns `count > 60, lag < 5s` within 90 seconds of `docker compose up`. Zero silent zeros in any column. Anomaly events flow end-to-end via mock detect.

---

## 2. What already exists (keep / rewrite)

| Component | Decision | Notes |
|---|---|---|
| `pipeline/scrape_to_kafka.py` | **Rewrite** | Silent-zero bug: `data.get(name, 0.0)` never matches real Prometheus metric names. |
| `pipeline/app.py` | **Rewrite** | Replace deque loop with real Bytewax stateful processor. |
| `serve/app.py` | **Keep as mock** | Mock `/detect` returns `{"anomaly": false}`. Phase 2 wires real models. |
| `docker-compose.v2.yml` | **Extend** | Add ClickHouse service; Redpanda already present. |
| `upf-sim` | **Keep unchanged** | Exposes real Prometheus metrics at `:8090/metrics`. |

---

## 3. Architecture

```
upf-sim:8090/metrics
       |
       v
scraper (pipeline/scrape_to_kafka.py)
 - parse ALL Prometheus metrics with labels
 - compute counter rates (delta_value / delta_time)
 - produce JSON to upf.metrics.raw (keyed by upf_id)
 - fail-loud on scrape error: log + backoff, never silent-zero
       |
       v
Redpanda  -- topic: upf.metrics.raw (keyed by upf_id, 1 msg/sec)
       |                        |
       v                        v
Bytewax processor         ClickHouse (Kafka table engine)
 keyed by upf_id            reads upf.metrics.raw directly
 512-step sliding window    materialized view -> upf_metrics MergeTree
 per channel per node       (seconds-fresh analytical store)
 checkpoint: SQLite
 -> upf.anomalies.critical --> ClickHouse Kafka engine
   (mock detect, Phase 1)      -> anomaly_events MergeTree
```

Two latency regimes, one Kafka path:
- **Control plane (sub-second):** scraper -> Bytewax -> anomaly topic. Never touches ClickHouse.
- **History / analytics (seconds-fresh):** ClickHouse reads directly from both topics.

---

## 4. Scraper design (`pipeline/scrape_to_kafka.py`)

### 4.1 Prometheus metric parsing

Parse raw Prometheus text with `prometheus_client.parser.text_string_to_metric_families`.
Flatten each `(metric_name, label_dict, value)` into a dot-free key:

```
port_bytes_count{dir="rx",iface="N3"}   ->  "port_bytes_N3_rx"
port_bytes_count{dir="tx",iface="N6"}   ->  "port_bytes_N6_tx"
port_dropped_count{dir="rx",iface="N3"} ->  "port_dropped_N3_rx"
upf_sim_scenario{mode="congestion"}     ->  "upf_sim_scenario_congestion"
pfcp_sessions_total{node_id="sim-upf-01"} -> upf_id field, not a metric column
go_goroutines                           ->  "go_goroutines"
```

`node_id` label is extracted as the `upf_id` field of the Kafka message.

### 4.2 Rate computation

Prometheus counters only increase. Rates computed between consecutive ticks:

```python
_prev: dict[str, tuple[float, float]] = {}  # key -> (value, timestamp)

def compute_rate(key, current_value, current_ts):
    if key not in _prev:
        _prev[key] = (current_value, current_ts)
        return None  # first tick: no rate yet
    prev_value, prev_ts = _prev[key]
    elapsed = current_ts - prev_ts
    if elapsed <= 0 or current_value < prev_value:
        _prev[key] = (current_value, current_ts)
        return None  # counter reset -- skip
    rate = (current_value - prev_value) / elapsed
    _prev[key] = (current_value, current_ts)
    return rate
```

Message includes BOTH raw counter AND computed rate:
- `"port_bytes_N3_rx": 1234567890`      (raw, for audit)
- `"port_bytes_N3_rx_rate": 12440.0`    (bytes/sec, for ML)

If rate is None (first tick or reset), scraper skips that message entirely.

### 4.3 Kafka message schema (`upf.metrics.raw`)

One JSON message per scrape tick. All numeric values float64. Key = `upf_id` bytes.

```json
{
  "upf_id": "sim-upf-01",
  "ts": 1751500010.000,
  "pfcp_sessions_total": 730141.0,
  "port_bytes_N3_rx": 1234567890.0,
  "port_bytes_N3_rx_rate": 12440.0,
  "port_bytes_N6_tx": 987654321.0,
  "port_bytes_N6_tx_rate": 9876.2,
  "port_pkts_N3_rx": 8901234.0,
  "port_pkts_N3_rx_rate": 890.1,
  "port_pkts_N6_tx": 7812345.0,
  "port_pkts_N6_tx_rate": 781.2,
  "port_dropped_N3_rx": 4200.0,
  "port_dropped_N3_rx_rate": 0.0,
  "port_dropped_N6_rx": 12.0,
  "port_dropped_N6_rx_rate": 0.0,
  "upf_sim_scenario_normal": 1.0,
  "upf_sim_scenario_congestion": 0.0,
  "upf_sim_scenario_flatline": 0.0,
  "upf_sim_scenario_spike": 0.0,
  "go_goroutines": 42.0,
  "go_heap_alloc_bytes": 2097152.0,
  "process_cpu_seconds_total": 1.234,
  "process_cpu_rate": 0.02
}
```

### 4.4 Error handling

- HTTP error / timeout: log warning, skip tick, backoff 5s.
- Parse error: log error, skip tick.
- Kafka produce error: log error, continue (Redpanda retries internally).
- Counter reset (value decreased): skip rate for that tick, log info.

Never produce a message with None or 0.0 standing in for a failed value.

---

## 5. Bytewax processor design (`pipeline/app.py`)

### 5.1 Dataflow structure (Bytewax 0.21.x)

```python
flow = Dataflow("upf-pipeline")
stream = op.input("kafka-in", flow, KafkaSource(
    brokers=["redpanda:9092"], topics=["upf.metrics.raw"]
))
parsed = op.map("parse", stream, lambda msg: json.loads(msg.value))
keyed  = op.key_on("key-by-upf", parsed, lambda m: m["upf_id"])
windowed = op.stateful_map("window", keyed, build_window, update_window)
full_windows = op.filter("full", windowed, lambda w: w.is_full())
detections   = op.map("detect", full_windows, call_detect)
anomalies    = op.filter("anomaly-only", detections, lambda d: d["anomaly"])
op.output("kafka-out", anomalies, KafkaSink(
    brokers=["redpanda:9092"], topic="upf.anomalies.critical"
))
```

### 5.2 Window state

```python
ML_CHANNELS = [
    "pfcp_sessions_total", "port_bytes_N3_rx_rate", "port_bytes_N6_tx_rate",
    "port_pkts_N3_rx_rate", "port_dropped_N3_rx_rate", "port_dropped_N6_rx_rate",
    "go_goroutines", "go_heap_alloc_bytes", "process_cpu_rate",
    "upf_sim_scenario_congestion", "upf_sim_scenario_flatline",
    "upf_sim_scenario_spike", "port_pkts_N6_tx_rate", "port_dropped_N6_rx",
]
WINDOW_SIZE = 512
```

`WindowState` per upf_id: dict of `channel -> deque(maxlen=512)`.
- `is_full()` = all 14 channels have exactly 512 entries.
- `to_array()` returns `list[list[float]]` of shape `(14, 512)`.

### 5.3 Mock detect call (Phase 1)

```python
def call_detect(upf_id, window):
    try:
        resp = requests.post(
            f"{RAY_SERVE_URL}/detect",
            json={"channels": window.to_array(), "channel_names": ML_CHANNELS},
            timeout=5.0,
        )
        result = resp.json()
        result["upf_id"] = upf_id
        return result
    except Exception as e:
        log.warning(f"detect failed {upf_id}: {e}")
        return {"upf_id": upf_id, "anomaly": False, "anomaly_score": 0.0}
```

### 5.4 Checkpointing

Bytewax saves state (Kafka offset + window contents) to SQLite at `BYTEWAX_CHECKPOINT_DIR=/data/bytewax-checkpoint`. Mount a named Docker volume at `/data`. On restart, Bytewax resumes from the last checkpoint (within `BYTEWAX_RECOVERY_INTERVAL_SECS`, default 30s).

### 5.5 Anomaly event schema (`upf.anomalies.critical`)

```json
{
  "upf_id": "sim-upf-01",
  "ts": 1751500010.000,
  "anomaly": true,
  "anomaly_score": 0.87,
  "threshold": 0.75,
  "top_anomalous_channels": ["port_dropped_N3_rx_rate"],
  "channel_scores": {"port_dropped_N3_rx_rate": 0.91},
  "confidence": 0.85,
  "model_version": "v2-ray-serve@cuda",
  "window_end_offset": 15001
}
```

In Phase 1, `anomaly` is always `false` (mock). Schema defined now so ClickHouse table is ready for Phase 2.

---

## 6. ClickHouse design

### 6.1 Service

Single-node. Image: `clickhouse/clickhouse-server:24.3-alpine`.
Ports 8123 (HTTP) and 9000 (native TCP) — internal to v2-net only, not exposed to host.
Admin creds: `CLICKHOUSE_USER` / `CLICKHOUSE_PASSWORD` from `.env`.
Init SQL: `config/clickhouse/init.sql` mounted at `/docker-entrypoint-initdb.d/init.sql`.

### 6.2 Schema -- upf_metrics

```sql
CREATE TABLE IF NOT EXISTS upf_metrics_kafka (
    upf_id String, ts Float64,
    pfcp_sessions_total Float64,
    port_bytes_N3_rx Float64,          port_bytes_N3_rx_rate Float64,
    port_bytes_N6_tx Float64,          port_bytes_N6_tx_rate Float64,
    port_pkts_N3_rx Float64,           port_pkts_N3_rx_rate Float64,
    port_pkts_N6_tx Float64,           port_pkts_N6_tx_rate Float64,
    port_dropped_N3_rx Float64,        port_dropped_N3_rx_rate Float64,
    port_dropped_N6_rx Float64,        port_dropped_N6_rx_rate Float64,
    upf_sim_scenario_normal Float64,   upf_sim_scenario_congestion Float64,
    upf_sim_scenario_flatline Float64, upf_sim_scenario_spike Float64,
    go_goroutines Float64,             go_heap_alloc_bytes Float64,
    process_cpu_seconds_total Float64, process_cpu_rate Float64
) ENGINE = Kafka
SETTINGS kafka_broker_list = 'redpanda:9092',
         kafka_topic_list  = 'upf.metrics.raw',
         kafka_group_name  = 'clickhouse-metrics',
         kafka_format      = 'JSONEachRow',
         kafka_skip_broken_messages = 10;

CREATE TABLE IF NOT EXISTS upf_metrics (
    upf_id LowCardinality(String), ts DateTime64(3),
    pfcp_sessions_total Float64,
    port_bytes_N3_rx Float64,          port_bytes_N3_rx_rate Float64,
    port_bytes_N6_tx Float64,          port_bytes_N6_tx_rate Float64,
    port_pkts_N3_rx Float64,           port_pkts_N3_rx_rate Float64,
    port_pkts_N6_tx Float64,           port_pkts_N6_tx_rate Float64,
    port_dropped_N3_rx Float64,        port_dropped_N3_rx_rate Float64,
    port_dropped_N6_rx Float64,        port_dropped_N6_rx_rate Float64,
    upf_sim_scenario_normal Float64,   upf_sim_scenario_congestion Float64,
    upf_sim_scenario_flatline Float64, upf_sim_scenario_spike Float64,
    go_goroutines Float64,             go_heap_alloc_bytes Float64,
    process_cpu_seconds_total Float64, process_cpu_rate Float64
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(ts)
ORDER BY (upf_id, ts)
TTL ts + INTERVAL 90 DAY;

CREATE MATERIALIZED VIEW IF NOT EXISTS upf_metrics_mv TO upf_metrics AS
SELECT upf_id,
       fromUnixTimestamp64Milli(toInt64(ts * 1000)) AS ts,
       pfcp_sessions_total,
       port_bytes_N3_rx, port_bytes_N3_rx_rate,
       port_bytes_N6_tx, port_bytes_N6_tx_rate,
       port_pkts_N3_rx,  port_pkts_N3_rx_rate,
       port_pkts_N6_tx,  port_pkts_N6_tx_rate,
       port_dropped_N3_rx, port_dropped_N3_rx_rate,
       port_dropped_N6_rx, port_dropped_N6_rx_rate,
       upf_sim_scenario_normal, upf_sim_scenario_congestion,
       upf_sim_scenario_flatline, upf_sim_scenario_spike,
       go_goroutines, go_heap_alloc_bytes,
       process_cpu_seconds_total, process_cpu_rate
FROM upf_metrics_kafka;
```

### 6.3 Schema -- anomaly_events

```sql
CREATE TABLE IF NOT EXISTS anomaly_events_kafka (
    upf_id String, ts Float64,
    anomaly UInt8, anomaly_score Float64, threshold Float64,
    top_anomalous_channels Array(String), model_version String,
    window_end_offset Int64
) ENGINE = Kafka
SETTINGS kafka_broker_list = 'redpanda:9092',
         kafka_topic_list  = 'upf.anomalies.critical',
         kafka_group_name  = 'clickhouse-anomaly',
         kafka_format      = 'JSONEachRow',
         kafka_skip_broken_messages = 10;

CREATE TABLE IF NOT EXISTS anomaly_events (
    upf_id LowCardinality(String), ts DateTime64(3),
    anomaly UInt8, anomaly_score Float64, threshold Float64,
    top_anomalous_channels Array(String), model_version String,
    window_end_offset Int64
) ENGINE = ReplacingMergeTree(ts)
PARTITION BY toYYYYMMDD(ts)
ORDER BY (upf_id, ts)
TTL ts + INTERVAL 90 DAY;

CREATE MATERIALIZED VIEW IF NOT EXISTS anomaly_events_mv TO anomaly_events AS
SELECT upf_id,
       fromUnixTimestamp64Milli(toInt64(ts * 1000)) AS ts,
       anomaly, anomaly_score, threshold,
       top_anomalous_channels, model_version, window_end_offset
FROM anomaly_events_kafka;
```

### 6.4 Dynamic metrics handling

New metrics from upf-sim appear in the Kafka message but are ignored by the Kafka engine table
(unknown columns skipped with kafka_skip_broken_messages). To add a new metric to ClickHouse:
`ALTER TABLE upf_metrics ADD COLUMN new_metric Float64 DEFAULT 0` + same change to
`upf_metrics_kafka` and the materialized view. No scraper or Bytewax code changes needed.

---

## 7. Docker Compose changes (`docker-compose.v2.yml`)

New ClickHouse service:

```yaml
clickhouse:
  image: clickhouse/clickhouse-server:24.3-alpine
  environment:
    CLICKHOUSE_USER: ${CLICKHOUSE_USER:-chuser}
    CLICKHOUSE_PASSWORD: ${CLICKHOUSE_PASSWORD:-localdev123}
    CLICKHOUSE_DB: bess_upf
  volumes:
    - clickhouse-data:/var/lib/clickhouse
    - ./config/clickhouse/init.sql:/docker-entrypoint-initdb.d/init.sql:ro
  networks:
    - v2-net
  healthcheck:
    test: ["CMD", "clickhouse-client", "--query", "SELECT 1"]
    interval: 10s
    timeout: 5s
    retries: 5
    start_period: 30s
  deploy:
    resources:
      limits:
        cpus: "2.0"
        memory: 2G
      reservations:
        memory: 512M
```

New volume: `clickhouse-data:`.
New `.env` keys: `CLICKHOUSE_USER=chuser`, `CLICKHOUSE_PASSWORD=localdev123`.

Bytewax (rewritten `pipeline`) env vars:
- `RAY_SERVE_URL=http://serve:8000`
- `BYTEWAX_CHECKPOINT_DIR=/data/bytewax-checkpoint`
- Add volume `bytewax-data:/data` to the pipeline service.

---

## 8. Topic contract tests (`pipeline/tests/`)

**test_scraper_schema.py:**
Mock HTTP server returns known Prometheus text. Run `parse_metrics()`. Assert all expected
fields present, types are float, `upf_id` extracted from `node_id` label correctly, counter
reset produces None (no message sent).

**test_window_state.py:**
Feed 600 messages into `WindowState`. Assert `is_full()` becomes True at exactly 512 steps.
Assert `to_array()` returns shape `(14, 512)`. Assert missing channel key does not append.

**test_clickhouse_schema.py** (integration, requires ClickHouse running):
Insert one row via `clickhouse-connect`. Query it back. Assert timestamp roundtrip within 1ms.
Assert `pfcp_sessions_total` stored as Float64. Assert row count increases by 1.

---

## 9. File map

```
Create:
  pipeline/scrape_to_kafka.py              (full rewrite)
  pipeline/app.py                          (full rewrite -- Bytewax dataflow)
  pipeline/tests/__init__.py
  pipeline/tests/test_scraper_schema.py
  pipeline/tests/test_window_state.py
  pipeline/tests/test_clickhouse_schema.py
  config/clickhouse/init.sql               (DDL: all tables + materialized views)

Modify:
  pipeline/requirements.txt                (add bytewax==0.21.*, prometheus-client, clickhouse-connect)
  docker-compose.v2.yml                    (clickhouse service + volume + pipeline env vars)
  .env                                     (add CLICKHOUSE_USER, CLICKHOUSE_PASSWORD)
```

---

## 10. Phase 1 exit criteria

1. `docker compose -f docker-compose.v2.yml up -d` -- all services healthy within 90s.
2. `docker exec clickhouse clickhouse-client --query "SELECT count(*), now() - max(ts) AS lag FROM bess_upf.upf_metrics"` -> `count > 60`, `lag < 5s`.
3. `docker exec clickhouse clickhouse-client --query "SELECT count(*) FROM bess_upf.upf_metrics WHERE pfcp_sessions_total = 0"` -> `count = 0`.
4. `docker exec clickhouse clickhouse-client --query "SELECT count(*) FROM bess_upf.anomaly_events"` -> table exists (Phase 2 populates with real detections).
5. `pytest pipeline/tests/ -v` -> all 3 test files pass.

---

## 11. Out of scope (Phase 1)

- Real model inference in `serve/app.py` (Phase 2).
- Avro schema registry (deferred; JSON + fail-loud achieves contract for Phase 1).
- Analysis service PromQL -> ClickHouse SQL migration (Phase 2).
- ClickHouse HA/replication (Phase 4).
- Bytewax -> Ray Serve actor wiring (Phase 2).
