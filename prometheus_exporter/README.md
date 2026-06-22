# BESS UPF Metrics → CSV

Exports BESS UPF metrics from a running Prometheus instance to CSV files
suitable for ML model training and live monitoring.

---

## Architecture

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
   └── full_export.py               ├── watcher.py
       long-format, all labels      │   wide-format, WAL-triggered live
       as columns, full history     └── full_export.py
                                        long-format, all labels as columns
                                        WAL-triggered, mirrors watcher.py
          │                                │
          └───────────────┬────────────────┘
                          ▼
                       data/
```

| | `promql/full_export.py` | `promtool/watcher.py` | `promtool/full_export.py` |
|---|---|---|---|
| **Data path** | HTTP `/api/v1/query_range` | `docker exec promtool` | `docker exec promtool` |
| **Output format** | Long (each label = own column) | Wide (labels in column name) | Long (each label = own column) |
| **ML-ready** | Yes | No — needs reshaping | Yes |
| **Live / batch** | Batch (full history in one run) | Live, WAL-triggered | Live, WAL-triggered |
| **Works if HTTP down** | No | Yes | Yes |
| **History depth** | Full retention (`--days`) | Seed window (`--hours`) | Seed window (`--hours`) |

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
source venv/bin/activate        # bash/zsh
# source venv/bin/activate.fish # fish shell
pip install -r requirements.txt
```

---

## Running

### One-shot ML export — full history via HTTP API

Queries the full Prometheus retention window in one run.
Best when you want to export a large historical slice for offline ML training.

```bash
# All metrics, last 14 days, 60s step
python promql/full_export.py

# Specific metric families, finer resolution
python promql/full_export.py --prefix upf_ pfcp_ --days 7 --step 30s

# Drop high-noise constant labels, custom output
python promql/full_export.py --exclude-labels instance job --out data/train.csv

# Remote Prometheus
python promql/full_export.py --prom-url http://192.168.1.10:9090
```

Output: `data/prometheus_full_export.csv`

See [`promql/full_export.py`](promql/full_export.py) `--help` for all flags.

---

### Live CSV — promtool approach (direct TSDB read)

Polls continuously, appending new rows as WAL segments are written.
Best for live training data collection alongside a running UPF.

```bash
# Wide-format live watcher
python promtool/watcher.py                       # 15s WAL-triggered poll
python promtool/watcher.py --interval 30
python promtool/watcher.py --hours 336           # seed 14 days on first run

# Long-format ML watcher (all labels as columns)
python promtool/full_export.py                   # 15s WAL-triggered poll
python promtool/full_export.py --hours 336       # seed 14 days on first run
python promtool/full_export.py --include-prefix upf_ pfcp_
python promtool/full_export.py --exclude-labels instance job
```

Output: `data/promtool_live_*.csv` / `data/ml_export_*.csv`

See [`promtool/README.md`](promtool/README.md) for all options and filter configuration.

---

### Generate varied traffic

Run in a separate terminal while a watcher is collecting:

```bash
python generate_traffic.py              # BURST / NORMAL / IDLE forever
python generate_traffic.py --cycles 20  # stop after 20 cycles
```

---

## Output files

All CSVs land in `data/`:

| File pattern | Produced by | Format |
|---|---|---|
| `prometheus_full_export.csv` | `promql/full_export.py` | Long, all labels as columns |
| `promtool_live_*.csv` | `promtool/watcher.py` | Wide, labels in column names |
| `ml_export_*.csv` | `promtool/full_export.py` | Long, all labels as columns |

### Long format (ML-ready)

One row per data point. Labels are individual columns — ready as categorical features:

```
timestamp_ms | datetime_utc        | metric_name         | value | instance       | job | iface  | dir
1750000000000 | 2025-06-15T12:00:00Z | upf_packets_count  | 42.0  | localhost:8080 | upf | Access | rx
1750000000000 | 2025-06-15T12:00:00Z | upf_dropped_count  | 1.0   | localhost:8080 | upf | Core   | tx
```

### Wide format (watcher.py)

One row per timestamp. Each (metric + label set) is a separate column:

```
timestamp            | upf_packets_count{dir=rx,iface=Access} | upf_dropped_count{dir=tx,iface=Core}
2025-06-15 12:00:00Z | 42.0                                   | 1.0
```

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

# Long-format output from promql/full_export.py or promtool/full_export.py
df = pd.read_csv("data/prometheus_full_export.csv")

# timestamp_ms is a Unix epoch integer — convert for time-series indexing
df["datetime_utc"] = pd.to_datetime(df["datetime_utc"], utc=True)

# Label columns (instance, job, iface, dir, ...) are strings — encode for sklearn
from sklearn.preprocessing import LabelEncoder
label_cols = [c for c in df.columns if c not in ("timestamp_ms", "datetime_utc", "value")]
for col in label_cols:
    df[col] = LabelEncoder().fit_transform(df[col].fillna(""))

print(df.head())
print(df.dtypes)
```

---

## File Structure

```
prometheus_exporter/
├── promql/
│   └── full_export.py       # HTTP API → long-format ML CSV (batch, full history)
├── promtool/
│   ├── watcher.py           # docker exec promtool → wide-format CSV (live)
│   ├── full_export.py       # docker exec promtool → long-format ML CSV (live)
│   └── README.md
├── data/                    # all CSV output (auto-created)
├── requirements.txt         # requests, pandas
└── README.md
```

> `UPF/` and the `prom` container are never modified.
