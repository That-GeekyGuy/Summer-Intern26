# Phase 1: Real Streaming Backbone Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace silent-zero stub pipeline with a real streaming backbone so that real sim data flows end-to-end with zero silent zeros and lands durably in ClickHouse.

**Architecture:** Scraper parses all Prometheus metrics with labels, computes counter rates (delta/time), and produces include-all JSON to `upf.metrics.raw` keyed by `upf_id`. Bytewax reads that topic, maintains a 512-step sliding window per UPF, calls mock detect when full, and publishes anomaly events. ClickHouse reads both topics via Kafka table engine (no extra consumer code) and persists rows in MergeTree via materialized views.

**Tech Stack:** Python 3.10, `prometheus-client` (Prometheus text parser), `bytewax==0.21.*` (stateful stream processor), `confluent-kafka` (Redpanda), `clickhouse-connect` (ClickHouse HTTP client), `pytest`, Docker Compose v2.

---

## File Map

```
Create:
  pipeline/window.py                       WindowState class + ML_CHANNELS + WINDOW_SIZE
  pipeline/tests/__init__.py               empty
  pipeline/tests/test_scraper_schema.py    unit tests: parse + flatten + compute_rate
  pipeline/tests/test_window_state.py      unit tests: WindowState
  pipeline/tests/test_clickhouse.py        integration test (requires CH running)
  config/clickhouse/init.sql               DDL: Kafka engine tables + MergeTree + MAT VIEWS

Rewrite (same filenames):
  pipeline/scrape_to_kafka.py
  pipeline/app.py

Modify:
  pipeline/requirements.txt                bytewax 0.21, add prometheus-client + clickhouse-connect
  pipeline/start.sh                        use bytewax.run for app.py
  docker-compose.v2.yml                    add clickhouse service + volumes + pipeline env
  .env                                     add CLICKHOUSE_USER + CLICKHOUSE_PASSWORD
```

---

## Task 1: Update requirements and verify Dockerfile builds

**Files:**
- Modify: `pipeline/requirements.txt`

`bytewax==0.18.0` in the current file uses a method-chaining API that does not exist in 0.21. The operator-based API (`bytewax.operators`) was introduced in 0.19 and stabilised in 0.21. The image bakes deps at build time so this must be fixed before writing any new code.

- [ ] **Step 1: Replace `pipeline/requirements.txt`**

```
bytewax==0.21.0
confluent-kafka>=2.3
requests>=2.31
prometheus-client>=0.20
clickhouse-connect>=0.7
pytest>=8.0
```

- [ ] **Step 2: Verify Dockerfile builds**

```bash
docker build -t pipeline-test ./pipeline
```

Expected: image builds with no import errors. If `bytewax==0.21.0` is not on PyPI, check with:
```bash
pip index versions bytewax
```
and pin to the highest 0.21.x available.

- [ ] **Step 3: Smoke-test the import**

```bash
docker run --rm pipeline-test python -c "import bytewax.operators as op; print('ok')"
```

Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add pipeline/requirements.txt
git commit -m "feat(pipeline): upgrade bytewax 0.18->0.21, add prometheus-client + clickhouse-connect"
```

---

## Task 2: Scraper tests (TDD — write before implementation)

**Files:**
- Create: `pipeline/tests/__init__.py`
- Create: `pipeline/tests/test_scraper_schema.py`

These tests define the exact API shape of `parse_prometheus_text()` and `compute_rate()` before they exist. Running them now should produce ImportError, confirming the tests are wired to the right target.

- [ ] **Step 1: Create empty `pipeline/tests/__init__.py`**

Touch the file — no content needed.

- [ ] **Step 2: Write `pipeline/tests/test_scraper_schema.py`**

```python
# pipeline/tests/test_scraper_schema.py
"""
Unit tests for parse_prometheus_text() and compute_rate() in scrape_to_kafka.py.
Run from repo root: PYTHONPATH=. pytest pipeline/tests/test_scraper_schema.py -v
"""
import pytest

SAMPLE_PROM_TEXT = """
# HELP pfcp_sessions_total Total PFCP sessions
# TYPE pfcp_sessions_total counter
pfcp_sessions_total{node_id="sim-upf-01"} 730141

# HELP port_bytes_count Total port bytes
# TYPE port_bytes_count counter
port_bytes_count{dir="rx",iface="N3",node_id="sim-upf-01"} 1234567890
port_bytes_count{dir="tx",iface="N6",node_id="sim-upf-01"} 987654321

# HELP port_packets_count Total port packets
# TYPE port_packets_count counter
port_packets_count{dir="rx",iface="N3",node_id="sim-upf-01"} 8901234
port_packets_count{dir="tx",iface="N6",node_id="sim-upf-01"} 7812345

# HELP port_dropped_count Dropped packets
# TYPE port_dropped_count counter
port_dropped_count{dir="rx",iface="N3",node_id="sim-upf-01"} 4200
port_dropped_count{dir="rx",iface="N6",node_id="sim-upf-01"} 12

# HELP upf_sim_scenario Current scenario
# TYPE upf_sim_scenario gauge
upf_sim_scenario{mode="normal"} 1
upf_sim_scenario{mode="congestion"} 0
upf_sim_scenario{mode="flatline"} 0
upf_sim_scenario{mode="spike"} 0

# HELP go_goroutines Number of goroutines
# TYPE go_goroutines gauge
go_goroutines 42

# HELP go_heap_alloc_bytes Heap bytes allocated
# TYPE go_heap_alloc_bytes gauge
go_heap_alloc_bytes 2097152

# HELP process_cpu_seconds_total CPU seconds used
# TYPE process_cpu_seconds_total counter
process_cpu_seconds_total 1.234
"""


def test_parse_returns_upf_id():
    from pipeline.scrape_to_kafka import parse_prometheus_text
    _, upf_id = parse_prometheus_text(SAMPLE_PROM_TEXT)
    assert upf_id == "sim-upf-01"


def test_parse_port_bytes_rx():
    from pipeline.scrape_to_kafka import parse_prometheus_text
    result, _ = parse_prometheus_text(SAMPLE_PROM_TEXT)
    assert "port_bytes_N3_rx" in result
    assert result["port_bytes_N3_rx"] == 1234567890.0


def test_parse_port_bytes_tx():
    from pipeline.scrape_to_kafka import parse_prometheus_text
    result, _ = parse_prometheus_text(SAMPLE_PROM_TEXT)
    assert "port_bytes_N6_tx" in result
    assert result["port_bytes_N6_tx"] == 987654321.0


def test_parse_port_pkts():
    from pipeline.scrape_to_kafka import parse_prometheus_text
    result, _ = parse_prometheus_text(SAMPLE_PROM_TEXT)
    assert "port_pkts_N3_rx" in result
    assert "port_pkts_N6_tx" in result


def test_parse_port_dropped():
    from pipeline.scrape_to_kafka import parse_prometheus_text
    result, _ = parse_prometheus_text(SAMPLE_PROM_TEXT)
    assert "port_dropped_N3_rx" in result
    assert result["port_dropped_N6_rx"] == 12.0


def test_parse_scenario_keys():
    from pipeline.scrape_to_kafka import parse_prometheus_text
    result, _ = parse_prometheus_text(SAMPLE_PROM_TEXT)
    assert result["upf_sim_scenario_normal"] == 1.0
    assert result["upf_sim_scenario_congestion"] == 0.0
    assert "upf_sim_scenario_flatline" in result
    assert "upf_sim_scenario_spike" in result


def test_parse_gauge_keys():
    from pipeline.scrape_to_kafka import parse_prometheus_text
    result, _ = parse_prometheus_text(SAMPLE_PROM_TEXT)
    assert result["go_goroutines"] == 42.0
    assert result["go_heap_alloc_bytes"] == 2097152.0


def test_parse_pfcp():
    from pipeline.scrape_to_kafka import parse_prometheus_text
    result, _ = parse_prometheus_text(SAMPLE_PROM_TEXT)
    assert "pfcp_sessions_total" in result
    assert result["pfcp_sessions_total"] == 730141.0


def test_no_rate_keys_from_parse():
    """parse_prometheus_text must NOT produce _rate keys — that is compute_rate's job."""
    from pipeline.scrape_to_kafka import parse_prometheus_text
    result, _ = parse_prometheus_text(SAMPLE_PROM_TEXT)
    assert "port_bytes_N3_rx_rate" not in result
    assert "process_cpu_rate" not in result


def test_rate_none_on_first_tick():
    from pipeline.scrape_to_kafka import compute_rate
    prev: dict = {}
    assert compute_rate("pfcp_sessions_total", 730141.0, 1000.0, prev) is None


def test_rate_computed_on_second_tick():
    from pipeline.scrape_to_kafka import compute_rate
    prev: dict = {}
    compute_rate("pfcp_sessions_total", 730000.0, 1000.0, prev)
    rate = compute_rate("pfcp_sessions_total", 730060.0, 1001.0, prev)
    assert rate == pytest.approx(60.0)


def test_counter_reset_returns_none():
    from pipeline.scrape_to_kafka import compute_rate
    prev: dict = {}
    compute_rate("pfcp_sessions_total", 730141.0, 1000.0, prev)
    # value decreased = counter reset (process restarted)
    assert compute_rate("pfcp_sessions_total", 100.0, 1001.0, prev) is None
```

- [ ] **Step 3: Run to confirm ImportError**

```bash
pip install prometheus-client pytest
PYTHONPATH=. pytest pipeline/tests/test_scraper_schema.py -v 2>&1 | head -10
```

Expected: `ImportError: cannot import name 'parse_prometheus_text'`

- [ ] **Step 4: Commit the tests**

```bash
git add pipeline/tests/__init__.py pipeline/tests/test_scraper_schema.py
git commit -m "test(scraper): failing tests for parse_prometheus_text and compute_rate"
```

---

## Task 3: Scraper implementation

**Files:**
- Rewrite: `pipeline/scrape_to_kafka.py`

Three functions replace the old monolith: `flatten_key` maps Prometheus sample names + labels to flat keys, `parse_prometheus_text` drives it over the whole metric text, `compute_rate` keeps previous counter values and computes per-second rates.

- [ ] **Step 1: Write `pipeline/scrape_to_kafka.py`**

```python
# pipeline/scrape_to_kafka.py
import json
import logging
import os
import time

import requests
from confluent_kafka import Producer
from prometheus_client.parser import text_string_to_metric_families

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

KAFKA_BROKER = os.getenv("KAFKA_BROKER", "redpanda:9092")
UPF_SIM_METRICS = os.getenv("UPF_SIM_METRICS", "http://upf-sim:8090/metrics")
SCRAPE_INTERVAL = float(os.getenv("SCRAPE_INTERVAL_SECS", "1.0"))

_PORT_FAMILIES: dict[str, str] = {
    "port_bytes_count": "port_bytes",
    "port_packets_count": "port_pkts",
    "port_dropped_count": "port_dropped",
}

_COUNTER_PREFIXES = (
    "pfcp_sessions_total",
    "port_bytes_",
    "port_pkts_",
    "port_dropped_",
    "process_cpu_seconds_total",
)


def flatten_key(sample_name: str, labels: dict) -> tuple[str | None, str | None]:
    """
    Map a Prometheus sample name + label dict to a flat snake_case key.
    Returns (metric_key, upf_id). Returns (None, None) to skip this sample.
    node_id label is extracted as upf_id and not included in the key.
    """
    # Skip histogram/summary internals (not from port_* families)
    for suffix in ("_bucket", "_sum", "_count"):
        if sample_name.endswith(suffix) and not any(
            sample_name.startswith(p) for p in _PORT_FAMILIES
        ):
            return None, None

    upf_id: str | None = labels.get("node_id")
    clean = {k: v for k, v in labels.items() if k != "node_id"}

    for full_prefix, short in _PORT_FAMILIES.items():
        if sample_name.startswith(full_prefix):
            iface = clean.get("iface", "")
            dir_ = clean.get("dir", "")
            return f"{short}_{iface}_{dir_}", upf_id

    if sample_name.startswith("upf_sim_scenario"):
        return f"upf_sim_scenario_{clean.get('mode', 'unknown')}", upf_id

    if not clean:
        return sample_name, upf_id

    suffix_val = "_".join(v for _, v in sorted(clean.items()))
    return f"{sample_name}_{suffix_val}", upf_id


def parse_prometheus_text(text: str) -> tuple[dict[str, float], str | None]:
    """
    Parse Prometheus exposition text into a flat dict.
    Returns (metrics_dict, upf_id).
    Does NOT compute rates.
    """
    result: dict[str, float] = {}
    upf_id: str | None = None

    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            key, uid = flatten_key(sample.name, sample.labels)
            if key is None:
                continue
            if uid is not None:
                upf_id = uid
            result[key] = sample.value

    return result, upf_id


def compute_rate(
    key: str,
    current_value: float,
    current_ts: float,
    prev: dict[str, tuple[float, float]],
) -> float | None:
    """
    Compute per-second rate for a counter metric.
    Mutates prev in place. Returns None on first tick or counter reset.
    Never returns 0.0 as a fallback for missing data.
    """
    if key not in prev:
        prev[key] = (current_value, current_ts)
        return None

    prev_value, prev_ts = prev[key]
    elapsed = current_ts - prev_ts

    if elapsed <= 0 or current_value < prev_value:
        prev[key] = (current_value, current_ts)
        log.info("Counter reset or zero elapsed for %s — skipping rate", key)
        return None

    rate = (current_value - prev_value) / elapsed
    prev[key] = (current_value, current_ts)
    return rate


def build_message(
    metrics: dict[str, float],
    upf_id: str,
    ts: float,
    prev_state: dict[str, tuple[float, float]],
) -> dict:
    msg: dict = {"upf_id": upf_id, "ts": ts}
    msg.update(metrics)

    for key, value in metrics.items():
        if any(key.startswith(p) for p in _COUNTER_PREFIXES):
            rate = compute_rate(key, value, ts, prev_state)
            if rate is not None:
                rate_key = key.replace("process_cpu_seconds_total", "process_cpu") + "_rate"
                msg[rate_key] = rate

    return msg


def main() -> None:
    producer = Producer({"bootstrap.servers": KAFKA_BROKER})
    log.info("Scraper started: %s -> upf.metrics.raw", UPF_SIM_METRICS)

    prev_state: dict[str, tuple[float, float]] = {}

    while True:
        tick_start = time.time()
        try:
            resp = requests.get(UPF_SIM_METRICS, timeout=5.0)
            resp.raise_for_status()

            metrics, upf_id = parse_prometheus_text(resp.text)
            if upf_id is None:
                log.warning("No node_id label in metrics — skipping tick")
            else:
                msg = build_message(metrics, upf_id, tick_start, prev_state)
                producer.produce(
                    "upf.metrics.raw",
                    key=upf_id.encode(),
                    value=json.dumps(msg).encode(),
                )
                producer.flush()

        except requests.RequestException as exc:
            log.warning("Scrape failed: %s — backing off 5s", exc)
            time.sleep(5.0)
            continue

        elapsed = time.time() - tick_start
        time.sleep(max(0.0, SCRAPE_INTERVAL - elapsed))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run scraper tests — all must pass**

```bash
PYTHONPATH=. pytest pipeline/tests/test_scraper_schema.py -v
```

Expected: 12 tests PASSED.

**If `test_parse_pfcp` fails:** newer `prometheus_client` parsers strip `_total` from family names. Print `[(s.name, s.labels) for f in text_string_to_metric_families(SAMPLE_PROM_TEXT) for s in f.samples]` and verify the actual sample name. Update the test assertion and `_COUNTER_PREFIXES` in `scrape_to_kafka.py` to match.

- [ ] **Step 3: Commit**

```bash
git add pipeline/scrape_to_kafka.py
git commit -m "feat(scraper): rewrite with label parser, counter rates, fail-loud errors"
```

---

## Task 4: WindowState module + tests

**Files:**
- Create: `pipeline/window.py`
- Create: `pipeline/tests/test_window_state.py`

`WindowState` holds one `deque(maxlen=512)` per ML channel. Bytewax checkpoints it to SQLite via pickle between recovery intervals. Both `app.py` and the tests import `ML_CHANNELS` from `pipeline.window` — single source of truth.

- [ ] **Step 1: Write failing tests first**

```python
# pipeline/tests/test_window_state.py
"""
Unit tests for WindowState in pipeline/window.py.
Run: PYTHONPATH=. pytest pipeline/tests/test_window_state.py -v
"""
from pipeline.window import WindowState, ML_CHANNELS, WINDOW_SIZE


def _msg(fill: float = 1.0) -> dict:
    return {ch: fill for ch in ML_CHANNELS}


def test_not_full_initially():
    assert not WindowState().is_full()


def test_full_at_window_size():
    ws = WindowState()
    for _ in range(WINDOW_SIZE):
        ws.update(_msg())
    assert ws.is_full()


def test_not_full_at_window_size_minus_one():
    ws = WindowState()
    for _ in range(WINDOW_SIZE - 1):
        ws.update(_msg())
    assert not ws.is_full()


def test_to_array_shape():
    ws = WindowState()
    for _ in range(WINDOW_SIZE):
        ws.update(_msg())
    arr = ws.to_array()
    assert len(arr) == len(ML_CHANNELS)
    assert all(len(row) == WINDOW_SIZE for row in arr)


def test_channel_values_in_correct_order():
    ws = WindowState()
    for _ in range(WINDOW_SIZE):
        msg = {ch: float(i) for i, ch in enumerate(ML_CHANNELS)}
        ws.update(msg)
    arr = ws.to_array()
    assert all(v == 0.0 for v in arr[0])
    assert all(v == 1.0 for v in arr[1])


def test_missing_key_does_not_append():
    ws = WindowState()
    msg = {ch: 1.0 for ch in ML_CHANNELS}
    del msg[ML_CHANNELS[0]]
    ws.update(msg)
    assert len(ws._channels[ML_CHANNELS[0]]) == 0
    assert len(ws._channels[ML_CHANNELS[1]]) == 1


def test_sliding_evicts_oldest():
    ws = WindowState()
    for i in range(WINDOW_SIZE):
        ws.update({ch: float(i) for ch in ML_CHANNELS})
    assert ws.to_array()[0][0] == 0.0
    ws.update({ch: 9999.0 for ch in ML_CHANNELS})
    assert ws.to_array()[0][0] == 1.0
```

- [ ] **Step 2: Confirm ModuleNotFoundError**

```bash
PYTHONPATH=. pytest pipeline/tests/test_window_state.py -v 2>&1 | head -5
```

Expected: `ModuleNotFoundError: No module named 'pipeline.window'`

- [ ] **Step 3: Write `pipeline/window.py`**

```python
# pipeline/window.py
from collections import deque

ML_CHANNELS: list[str] = [
    "pfcp_sessions_total",
    "port_bytes_N3_rx_rate",
    "port_bytes_N6_tx_rate",
    "port_pkts_N3_rx_rate",
    "port_dropped_N3_rx_rate",
    "port_dropped_N6_rx_rate",
    "go_goroutines",
    "go_heap_alloc_bytes",
    "process_cpu_rate",
    "upf_sim_scenario_congestion",
    "upf_sim_scenario_flatline",
    "upf_sim_scenario_spike",
    "port_pkts_N6_tx_rate",
    "port_dropped_N6_rx",
]

WINDOW_SIZE: int = 512


class WindowState:
    """
    Per-UPF sliding window. One deque per ML channel, maxlen=WINDOW_SIZE.
    Bytewax checkpoints this via pickle to SQLite on each recovery interval.
    """

    def __init__(self) -> None:
        self._channels: dict[str, deque[float]] = {
            ch: deque(maxlen=WINDOW_SIZE) for ch in ML_CHANNELS
        }

    def update(self, msg: dict) -> "WindowState":
        for ch in ML_CHANNELS:
            val = msg.get(ch)
            if val is not None:
                self._channels[ch].append(float(val))
        return self

    def is_full(self) -> bool:
        return all(len(q) == WINDOW_SIZE for q in self._channels.values())

    def to_array(self) -> list[list[float]]:
        """Returns list of shape (len(ML_CHANNELS), WINDOW_SIZE)."""
        return [list(self._channels[ch]) for ch in ML_CHANNELS]
```

- [ ] **Step 4: Run tests — all must pass**

```bash
PYTHONPATH=. pytest pipeline/tests/test_window_state.py -v
```

Expected: 7 tests PASSED.

- [ ] **Step 5: Commit**

```bash
git add pipeline/window.py pipeline/tests/test_window_state.py
git commit -m "feat(pipeline): WindowState 512-step keyed sliding window + unit tests"
```

---

## Task 5: Bytewax dataflow rewrite (`app.py`)

**Files:**
- Rewrite: `pipeline/app.py`
- Modify: `pipeline/start.sh`

Bytewax 0.21 discovers the flow via `python -m bytewax.run pipeline.app:flow`. The flow is built at module level. `op.stateful_map` receives `(state | None, value)` — `None` means first message for a new `upf_id`. SQLite checkpointing is automatic via the `BYTEWAX_CHECKPOINT_DIR` env var.

- [ ] **Step 1: Write `pipeline/app.py`**

```python
# pipeline/app.py
import json
import logging
import os

import requests
import bytewax.operators as op
from bytewax.connectors.kafka import KafkaSource, KafkaSink, KafkaSinkMessage
from bytewax.dataflow import Dataflow

from pipeline.window import WindowState, ML_CHANNELS

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

KAFKA_BROKER = os.getenv("KAFKA_BROKER", "redpanda:9092")
RAY_SERVE_URL = os.getenv("RAY_SERVE_URL", "http://serve:8000")


def _parse(raw_msg) -> dict:
    return json.loads(raw_msg.value)


def _window_mapper(
    state: WindowState | None, msg: dict
) -> tuple[WindowState, tuple[dict, list] | None]:
    if state is None:
        state = WindowState()
    state.update(msg)
    payload = (msg, state.to_array()) if state.is_full() else None
    return state, payload


def _call_detect(keyed: tuple[str, tuple[dict, list]]) -> dict | None:
    upf_id, (msg, channels_array) = keyed
    try:
        resp = requests.post(
            f"{RAY_SERVE_URL}/detect",
            json={"channels": channels_array, "channel_names": ML_CHANNELS},
            timeout=5.0,
        )
        resp.raise_for_status()
        result: dict = resp.json()
    except Exception as exc:
        log.warning("detect call failed for %s: %s", upf_id, exc)
        result = {"anomaly": False, "anomaly_score": 0.0}

    result["upf_id"] = upf_id
    result["ts"] = msg["ts"]
    return result if result.get("anomaly") else None


def _to_sink_msg(result: dict) -> KafkaSinkMessage:
    return KafkaSinkMessage(
        key=result["upf_id"].encode(),
        value=json.dumps(result).encode(),
    )


flow = Dataflow("upf-pipeline")

raw = op.input(
    "kafka-in",
    flow,
    KafkaSource(brokers=[KAFKA_BROKER], topics=["upf.metrics.raw"]),
)

parsed = op.map("parse", raw, _parse)
keyed = op.key_on("key-by-upf", parsed, lambda m: m["upf_id"])

# stateful_map: (state | None, value) -> (new_state, output | None)
windowed = op.stateful_map("window", keyed, _window_mapper)

# filter_map drops None (window not yet full for that upf_id)
snapshots = op.filter_map("drop-incomplete", windowed, lambda kv: kv[1])

detections = op.map("detect", snapshots, _call_detect)

# filter_map drops None (_call_detect returns None when anomaly=False)
anomalies = op.filter_map("anomaly-only", detections, lambda x: x)

op.output(
    "kafka-out",
    anomalies,
    KafkaSink(brokers=[KAFKA_BROKER], topic="upf.anomalies.critical"),
)
```

- [ ] **Step 2: Verify module imports cleanly**

```bash
PYTHONPATH=. python -c "from pipeline.app import flow; print('flow ok')"
```

Expected: `flow ok`

**If `ImportError: cannot import name 'KafkaSinkMessage'`:** check what the installed version exports:
```bash
python -c "import bytewax.connectors.kafka as k; print(dir(k))"
```
Adjust the import. Some versions use `KafkaMessage` or accept `(key, value)` directly as a tuple.

- [ ] **Step 3: Update `pipeline/start.sh`**

```sh
#!/bin/sh
python scrape_to_kafka.py &
python -m bytewax.run pipeline.app:flow
```

- [ ] **Step 4: Commit**

```bash
git add pipeline/app.py pipeline/start.sh
git commit -m "feat(pipeline): Bytewax 0.21 stateful dataflow with WindowState + mock detect"
```

---

## Task 6: ClickHouse DDL + docker-compose.v2.yml + .env

**Files:**
- Create: `config/clickhouse/init.sql`
- Modify: `docker-compose.v2.yml`
- Modify: `.env`

The Kafka engine table is not a storage table — it is a live stream reader. Every SELECT on it consumes messages from Redpanda. The MATERIALIZED VIEW fires on each batch ClickHouse reads and inserts into the MergeTree storage table. Unknown JSON fields from the scraper are silently ignored via `kafka_skip_broken_messages`.

- [ ] **Step 1: Create `config/clickhouse/init.sql`**

```sql
-- config/clickhouse/init.sql
CREATE DATABASE IF NOT EXISTS bess_upf;

USE bess_upf;

------------------------------------------------------------------------
-- upf_metrics
------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS upf_metrics_kafka (
    upf_id                     String,
    ts                         Float64,
    pfcp_sessions_total        Float64,
    port_bytes_N3_rx           Float64,  port_bytes_N3_rx_rate   Float64,
    port_bytes_N6_tx           Float64,  port_bytes_N6_tx_rate   Float64,
    port_pkts_N3_rx            Float64,  port_pkts_N3_rx_rate    Float64,
    port_pkts_N6_tx            Float64,  port_pkts_N6_tx_rate    Float64,
    port_dropped_N3_rx         Float64,  port_dropped_N3_rx_rate Float64,
    port_dropped_N6_rx         Float64,  port_dropped_N6_rx_rate Float64,
    upf_sim_scenario_normal    Float64,  upf_sim_scenario_congestion Float64,
    upf_sim_scenario_flatline  Float64,  upf_sim_scenario_spike  Float64,
    go_goroutines              Float64,  go_heap_alloc_bytes     Float64,
    process_cpu_seconds_total  Float64,  process_cpu_rate        Float64
) ENGINE = Kafka
SETTINGS
    kafka_broker_list          = 'redpanda:9092',
    kafka_topic_list           = 'upf.metrics.raw',
    kafka_group_name           = 'clickhouse-metrics',
    kafka_format               = 'JSONEachRow',
    kafka_skip_broken_messages = 10;

CREATE TABLE IF NOT EXISTS upf_metrics (
    upf_id                     LowCardinality(String),
    ts                         DateTime64(3),
    pfcp_sessions_total        Float64,
    port_bytes_N3_rx           Float64,  port_bytes_N3_rx_rate   Float64,
    port_bytes_N6_tx           Float64,  port_bytes_N6_tx_rate   Float64,
    port_pkts_N3_rx            Float64,  port_pkts_N3_rx_rate    Float64,
    port_pkts_N6_tx            Float64,  port_pkts_N6_tx_rate    Float64,
    port_dropped_N3_rx         Float64,  port_dropped_N3_rx_rate Float64,
    port_dropped_N6_rx         Float64,  port_dropped_N6_rx_rate Float64,
    upf_sim_scenario_normal    Float64,  upf_sim_scenario_congestion Float64,
    upf_sim_scenario_flatline  Float64,  upf_sim_scenario_spike  Float64,
    go_goroutines              Float64,  go_heap_alloc_bytes     Float64,
    process_cpu_seconds_total  Float64,  process_cpu_rate        Float64
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(ts)
ORDER BY (upf_id, ts)
TTL ts + INTERVAL 90 DAY;

CREATE MATERIALIZED VIEW IF NOT EXISTS upf_metrics_mv TO upf_metrics AS
SELECT
    upf_id,
    fromUnixTimestamp64Milli(toInt64(ts * 1000)) AS ts,
    pfcp_sessions_total,
    port_bytes_N3_rx,    port_bytes_N3_rx_rate,
    port_bytes_N6_tx,    port_bytes_N6_tx_rate,
    port_pkts_N3_rx,     port_pkts_N3_rx_rate,
    port_pkts_N6_tx,     port_pkts_N6_tx_rate,
    port_dropped_N3_rx,  port_dropped_N3_rx_rate,
    port_dropped_N6_rx,  port_dropped_N6_rx_rate,
    upf_sim_scenario_normal, upf_sim_scenario_congestion,
    upf_sim_scenario_flatline, upf_sim_scenario_spike,
    go_goroutines,       go_heap_alloc_bytes,
    process_cpu_seconds_total, process_cpu_rate
FROM upf_metrics_kafka;

------------------------------------------------------------------------
-- anomaly_events
------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS anomaly_events_kafka (
    upf_id                  String,
    ts                      Float64,
    anomaly                 UInt8,
    anomaly_score           Float64,
    threshold               Float64,
    top_anomalous_channels  Array(String),
    model_version           String,
    window_end_offset       Int64
) ENGINE = Kafka
SETTINGS
    kafka_broker_list          = 'redpanda:9092',
    kafka_topic_list           = 'upf.anomalies.critical',
    kafka_group_name           = 'clickhouse-anomaly',
    kafka_format               = 'JSONEachRow',
    kafka_skip_broken_messages = 10;

CREATE TABLE IF NOT EXISTS anomaly_events (
    upf_id                  LowCardinality(String),
    ts                      DateTime64(3),
    anomaly                 UInt8,
    anomaly_score           Float64,
    threshold               Float64,
    top_anomalous_channels  Array(String),
    model_version           String,
    window_end_offset       Int64
) ENGINE = ReplacingMergeTree(ts)
PARTITION BY toYYYYMMDD(ts)
ORDER BY (upf_id, ts)
TTL ts + INTERVAL 90 DAY;

CREATE MATERIALIZED VIEW IF NOT EXISTS anomaly_events_mv TO anomaly_events AS
SELECT
    upf_id,
    fromUnixTimestamp64Milli(toInt64(ts * 1000)) AS ts,
    anomaly, anomaly_score, threshold,
    top_anomalous_channels, model_version, window_end_offset
FROM anomaly_events_kafka;
```

- [ ] **Step 2: Add ClickHouse to `docker-compose.v2.yml`**

In the `volumes:` section, add after `ray-data:`:
```yaml
  clickhouse-data:
  bytewax-data:
```

In the `services:` section, add after `mitigation:`:
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

Replace the existing `pipeline:` service with:
```yaml
  pipeline:
    build:
      context: ./pipeline
      dockerfile: Dockerfile
    environment:
      - KAFKA_BROKER=redpanda:9092
      - RAY_SERVE_URL=http://serve:8000
      - BYTEWAX_CHECKPOINT_DIR=/data/bytewax-checkpoint
      - SCRAPE_INTERVAL_SECS=1.0
    volumes:
      - bytewax-data:/data
    depends_on:
      redpanda:
        condition: service_healthy
    networks:
      - v2-net
```

- [ ] **Step 3: Append to `.env`**

Add these two lines at the end of `.env`:
```
CLICKHOUSE_USER=chuser
CLICKHOUSE_PASSWORD=localdev123
```

- [ ] **Step 4: Validate compose**

```bash
docker compose -f docker-compose.v2.yml config --quiet
```

Expected: no output (no errors).

- [ ] **Step 5: Start ClickHouse and verify DDL**

```bash
docker compose -f docker-compose.v2.yml up -d clickhouse
```

Wait 35 seconds, then:
```bash
docker exec $(docker ps -qf name=clickhouse) clickhouse-client --query "SHOW TABLES IN bess_upf"
```

Expected (6 lines):
```
anomaly_events
anomaly_events_kafka
anomaly_events_mv
upf_metrics
upf_metrics_kafka
upf_metrics_mv
```

If empty: `docker logs <clickhouse-container-id>` to see SQL errors from init.sql.

- [ ] **Step 6: Commit**

```bash
git add config/clickhouse/init.sql docker-compose.v2.yml .env
git commit -m "feat(clickhouse): Kafka engine sink + MergeTree storage for metrics and anomaly events"
```

---

## Task 7: ClickHouse integration test + exit criterion

**Files:**
- Create: `pipeline/tests/test_clickhouse.py`

Requires ClickHouse running on `localhost:8123` from Task 6. Inserts one row directly via `clickhouse-connect` HTTP client, queries it back, checks timestamp roundtrip and that zero-pfcp rows do not exist.

- [ ] **Step 1: Write `pipeline/tests/test_clickhouse.py`**

```python
# pipeline/tests/test_clickhouse.py
"""
Integration test for ClickHouse schema correctness.
Requires ClickHouse: docker compose -f docker-compose.v2.yml up -d clickhouse

Run: PYTHONPATH=. pytest pipeline/tests/test_clickhouse.py -v
"""
import datetime
import time
import pytest

try:
    import clickhouse_connect
    CH_AVAILABLE = True
except ImportError:
    CH_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not CH_AVAILABLE, reason="clickhouse-connect not installed"
)


@pytest.fixture(scope="module")
def ch():
    try:
        client = clickhouse_connect.get_client(
            host="localhost",
            port=8123,
            username="chuser",
            password="localdev123",
            database="bess_upf",
        )
        client.ping()
        return client
    except Exception as exc:
        pytest.skip(f"ClickHouse not reachable at localhost:8123: {exc}")


def test_tables_exist(ch):
    result = ch.query("SHOW TABLES IN bess_upf")
    tables = {row[0] for row in result.result_rows}
    assert "upf_metrics" in tables
    assert "anomaly_events" in tables
    assert "upf_metrics_kafka" in tables


def test_insert_roundtrip(ch):
    ts_before = datetime.datetime.utcnow().replace(microsecond=0)

    ch.insert(
        "upf_metrics",
        [[
            "sim-upf-test", ts_before,
            730141.0,
            1234567890.0, 12440.0,
            987654321.0, 9876.2,
            8901234.0, 890.1,
            7812345.0, 781.2,
            4200.0, 0.0,
            12.0, 0.0,
            1.0, 0.0, 0.0, 0.0,
            42.0, 2097152.0,
            1.234, 0.02,
        ]],
        column_names=[
            "upf_id", "ts",
            "pfcp_sessions_total",
            "port_bytes_N3_rx", "port_bytes_N3_rx_rate",
            "port_bytes_N6_tx", "port_bytes_N6_tx_rate",
            "port_pkts_N3_rx", "port_pkts_N3_rx_rate",
            "port_pkts_N6_tx", "port_pkts_N6_tx_rate",
            "port_dropped_N3_rx", "port_dropped_N3_rx_rate",
            "port_dropped_N6_rx", "port_dropped_N6_rx_rate",
            "upf_sim_scenario_normal", "upf_sim_scenario_congestion",
            "upf_sim_scenario_flatline", "upf_sim_scenario_spike",
            "go_goroutines", "go_heap_alloc_bytes",
            "process_cpu_seconds_total", "process_cpu_rate",
        ],
    )

    time.sleep(1)

    result = ch.query(
        "SELECT upf_id, pfcp_sessions_total, ts "
        "FROM upf_metrics WHERE upf_id = 'sim-upf-test' "
        "ORDER BY ts DESC LIMIT 1"
    )
    assert result.row_count == 1
    row = result.first_row
    assert row[0] == "sim-upf-test"
    assert row[1] == pytest.approx(730141.0)

    stored_ts: datetime.datetime = row[2]
    diff = abs((stored_ts.replace(tzinfo=None) - ts_before).total_seconds())
    assert diff < 1.0, f"Timestamp roundtrip drift: {diff}s"


def test_no_pfcp_zero_rows(ch):
    """Guard: no row should have pfcp_sessions_total = 0 (silent zero indicator)."""
    result = ch.query(
        "SELECT count(*) FROM upf_metrics WHERE pfcp_sessions_total = 0"
    )
    count = result.first_row[0]
    assert count == 0, (
        f"{count} rows with pfcp_sessions_total=0 — "
        "old stub pipeline may have written silent zeros to the topic"
    )
```

- [ ] **Step 2: Run integration test**

```bash
pip install clickhouse-connect
PYTHONPATH=. pytest pipeline/tests/test_clickhouse.py -v
```

Expected: 3 tests PASSED.

**If `test_no_pfcp_zero_rows` fails:** flush the topic and restart:
```bash
docker exec $(docker ps -qf name=redpanda) rpk topic delete upf.metrics.raw
docker exec $(docker ps -qf name=redpanda) rpk topic create upf.metrics.raw
docker compose -f docker-compose.v2.yml restart pipeline
```
Wait 90s and re-run.

- [ ] **Step 3: Full-stack exit criterion**

Start the complete v2 stack:
```bash
docker compose -f docker-compose.v2.yml up -d
```

Wait 90 seconds, then verify all four exit criteria:

**1. Data flowing, lag < 5s:**
```bash
docker exec $(docker ps -qf name=clickhouse) clickhouse-client \
  --query "SELECT count(*), now() - max(ts) AS lag FROM bess_upf.upf_metrics"
```
Pass: `count > 60` AND `lag < 5`

**2. No silent zeros:**
```bash
docker exec $(docker ps -qf name=clickhouse) clickhouse-client \
  --query "SELECT count(*) FROM bess_upf.upf_metrics WHERE pfcp_sessions_total = 0"
```
Pass: `0`

**3. Anomaly table exists:**
```bash
docker exec $(docker ps -qf name=clickhouse) clickhouse-client \
  --query "SELECT count(*) FROM bess_upf.anomaly_events"
```
Pass: any number (mock detect always returns anomaly=false in Phase 1)

**4. All unit tests pass:**
```bash
PYTHONPATH=. pytest pipeline/tests/test_scraper_schema.py pipeline/tests/test_window_state.py -v
```
Pass: 19 tests PASSED

- [ ] **Step 4: Commit**

```bash
git add pipeline/tests/test_clickhouse.py
git commit -m "test(clickhouse): integration roundtrip + no-silent-zero guard"
```

---

## Self-Review

**Spec coverage check:**

| Spec section | Task |
|---|---|
| §4.1 Parse all Prometheus metrics with labels | Tasks 2 + 3 (parse_prometheus_text) |
| §4.2 Counter rate computation | Tasks 2 + 3 (compute_rate) |
| §4.3 Include-all JSON Kafka schema | Task 3 (build_message) |
| §4.4 Fail-loud — no silent zeros | Task 2 (test_no_rate_keys_from_parse) + Task 3 (raise_for_status) |
| §5.1 Bytewax 0.21 dataflow | Task 5 (app.py) |
| §5.2 512-step window keyed by upf_id | Tasks 4 + 5 (WindowState + op.stateful_map) |
| §5.4 SQLite checkpointing | Task 5 (BYTEWAX_CHECKPOINT_DIR + bytewax-data volume) |
| §5.5 Anomaly event schema | Task 5 (_call_detect return shape) |
| §6.1 ClickHouse single-node service | Task 6 (docker-compose.v2.yml) |
| §6.2 upf_metrics DDL | Task 6 (init.sql) |
| §6.3 anomaly_events DDL | Task 6 (init.sql) |
| §7 docker-compose.v2.yml changes | Task 6 |
| §8 Contract tests | Tasks 2, 4, 7 |
| §10 Exit criteria | Task 7 Step 3 |

All 14 spec sections covered.

**Placeholder scan:** No TBD, no "implement later", no "add error handling" without code.

**Type consistency:**
- `parse_prometheus_text` returns `(dict[str, float], str | None)` — matches test call `result, upf_id = parse_prometheus_text(...)` in all 12 tests.
- `compute_rate(key, value, ts, prev)` — 4-arg signature used consistently in Task 2 tests and Task 3 impl.
- `WindowState._channels` — accessed directly in `test_missing_key_does_not_append` (Tasks 4).
- `ML_CHANNELS` exported from `pipeline.window`, imported in `pipeline.app` and `test_window_state.py` — single definition.
- `KafkaSinkMessage` — Task 5 Step 2 includes explicit fallback instruction if import name differs across bytewax patch versions.
