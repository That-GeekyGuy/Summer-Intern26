# BESS UPF Metrics → CSV

Exports BESS UPF metrics from a running Prometheus instance to CSV.
Two independent approaches are provided for comparison.

---

## Approaches

```
┌──────────────────────────────────────────────────────────────────┐
│  BESS UPF  →  prom container :9090  (started by telemetry.sh)   │
└─────────────────────────┬────────────────────────────────────────┘
                          │
          ┌───────────────┴────────────────┐
          │                                │
          ▼                                ▼
   promql/                          promtool/
   HTTP API → Prometheus engine     docker exec → TSDB blocks + WAL
   ├── dump.py     (raw, watch)      └── watcher.py  (WAL-mtime triggered)
   └── ml_export.py (diff'd, ML)
          │                                │
          └───────────────┬────────────────┘
                          ▼
                       data/
```

| | `promql/dump.py --watch` | `promql/ml_export.py` | `promtool/watcher.py` |
|---|---|---|---|
| **Values** | Raw cumulative | Per-step delta | Raw cumulative |
| **Live append** | Yes | No (one-shot) | Yes |
| **Change detection** | Timer | — | WAL mtime |
| **Works if HTTP down** | No | No | Yes |
| **Good for** | Debug / snapshot | ML training | Comparison / offline |

---

## Startup Sequence

### 1 — Hugepages (once per boot, required by DPDK)

```bash
sudo sync && echo 3 | sudo tee /proc/sys/vm/drop_caches
echo 1 | sudo tee /proc/sys/vm/compact_memory
echo 512 | sudo tee /proc/sys/vm/nr_hugepages
```

### 2 — BESS UPF

```bash
cd UPF
DOCKER_BUILDKIT=0 ./scripts/docker_setup.sh
docker exec bess ./bessctl run up4
docker exec bess-pfcp pfcpiface -config /conf/upf.jsonc -simulate create
```

### 3 — Prometheus + Grafana

```bash
# From UPF/
./scripts/telemetry.sh
# Prometheus → http://localhost:9090
# Grafana    → http://localhost:3000
```

Wait ~15 seconds for the first scrape.

### 4 — Python environment

```bash
# From prometheus_exporter/
python -m venv venv
source venv/bin/activate.fish   # fish shell
pip install -r requirements.txt
```

---

## Running

### Live CSV — PromQL approach (HTTP API)

```bash
# Raw values, auto-append every 30s
python promql/dump.py --watch

# Faster polling (matches scrape interval)
python promql/dump.py --watch --interval 15

# ML-ready snapshot (one-shot, counter-diff'd)
python promql/ml_export.py --hours 24 --step 1m
```

See [`promql/README.md`](promql/README.md) for all options.

### Live CSV — promtool approach (direct TSDB read)

```bash
python promtool/watcher.py              # 15s WAL-triggered poll
python promtool/watcher.py --interval 30
```

See [`promtool/README.md`](promtool/README.md) for all options.

### Generate varied traffic

Run in a separate terminal while a watcher is collecting:

```bash
python generate_traffic.py              # BURST / NORMAL / IDLE forever
python generate_traffic.py --cycles 20 # stop after 20 cycles
python generate_traffic.py --export    # also call ml_export.py each cycle
```

---

## Output files

All CSVs land in `data/`:

| File pattern | Produced by |
|---|---|
| `prom_dump_*.csv` | `promql/dump.py` (snapshot) |
| `prom_live_*.csv` | `promql/dump.py --watch` |
| `ml_export_*.csv` | `promql/ml_export.py` |
| `promtool_live_*.csv` | `promtool/watcher.py` |

---

## Metrics

| Metric | Type | Note |
|---|---|---|
| `pfcp_sessions` | Gauge | Active PFCP sessions |
| `pfcp_messages_total` | Counter | Messages per type/direction |
| `pfcp_messages_duration_seconds_count` | Counter | PFCP request count |
| `pfcp_messages_duration_seconds_sum` | Counter | Cumulative request time |
| `upf_latency_ns_count` | Counter | Packets measured (throughput proxy) |
| `upf_latency_ns_sum` | Counter | Total latency |
| `upf_jitter_ns_count` | Counter | Same as latency_count |
| `upf_jitter_ns_sum` | Counter | Total jitter |

> **Sim mode**: `upf_packets_count`, `upf_bytes_count`, `upf_dropped_count`
> require real DPDK NICs — not emitted in sim mode.
> Latency/jitter quantiles are always 0 in sim; `_count` metrics are the
> meaningful throughput signal.

---

## Loading for ML

```python
import pandas as pd

# ml_export.py output — already diff'd, ready to use
df = pd.read_csv("data/ml_export_*.csv", index_col=0, parse_dates=True)
df = df.loc[:, (df != 0).any(axis=0)]   # drop always-zero sim artefacts
print(df.describe())
```

---

## File Structure

```
prometheus_exporter/
├── promql/
│   ├── dump.py          # HTTP API → CSV  (snapshot + --watch)
│   ├── ml_export.py     # HTTP API → ML-ready CSV  (diff'd counters)
│   ├── fetch.py         # dynamic metric discovery (used by ml_export)
│   ├── csv_writer.py    # write / append helpers
│   └── README.md
├── promtool/
│   ├── watcher.py       # docker exec promtool → CSV  (WAL-mtime triggered)
│   └── README.md
├── generate_traffic.py  # sim traffic generator (BURST / NORMAL / IDLE)
├── requirements.txt     # requests, pandas
└── data/                # all CSV output (auto-created)
```

> `UPF/` and the `prom` container are never modified.
