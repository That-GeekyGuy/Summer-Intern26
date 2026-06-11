# promql/ — Prometheus HTTP API → CSV

Two scripts, both querying the Prometheus HTTP API (`/api/v1/query_range`).

---

## Scripts

### `dump.py` — raw TSDB values

Direct copy of what Prometheus stores. Counters are cumulative (not diff'd).
Good for debugging, snapshots, or feeding tools that expect raw counters.

```bash
# One-off snapshot
python promql/dump.py                        # last 1h
python promql/dump.py --hours 6 --step 1m  # 6h at 1-min resolution
python promql/dump.py --split              # one CSV per metric

# Live auto-append (incremental, CSV is its own cursor)
python promql/dump.py --watch              # append every 30s
python promql/dump.py --watch --interval 15
```

Output: `data/prom_dump_YYYYMMDD_HHMMSS.csv` or `data/prom_live_YYYYMMDD_HHMMSS.csv`

---

### `ml_export.py` — ML-ready transformed values

Discovers metrics via `/api/v1/metadata`, applies counter-differencing
(raw cumulative → per-step delta), expands histograms to `_count`/`_sum`,
and computes derived `drop_ratio` columns.  Use this as input to ML models.

```bash
python promql/ml_export.py                       # last 1h
python promql/ml_export.py --hours 24 --step 1m # 24h training slice
```

Output: `data/ml_export_YYYYMMDD_HHMMSS.csv`

---

## Internal modules (not run directly)

| File | Purpose |
|---|---|
| `fetch.py` | Dynamic metric discovery + DataFrame builder (used by ml_export.py) |
| `csv_writer.py` | CSV write/append helpers |

---

## Comparison: dump.py vs ml_export.py

| | `dump.py` | `ml_export.py` |
|---|---|---|
| Counter values | Raw cumulative | Per-step delta (`.diff()`) |
| Live watch mode | Yes (`--watch`) | No (one-shot) |
| Metric discovery | `label/__name__/values` | `/api/v1/metadata` |
| Histogram handling | Skip `_bucket`, rest raw | Expand to `_count`/`_sum` |
| Good for | Debugging, raw storage | ML feature matrix |
