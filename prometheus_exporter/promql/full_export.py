#!/usr/bin/env python3
"""
full_export.py
──────────────
Export the full Prometheus TSDB to a long-format CSV for ML training.

Each row = one (timestamp, metric_name, value, ...label columns) data point.
Labels become individual columns — categorical features ready for model ingestion.

Why long format vs. the wide format used by dump.py / ml_export.py
───────────────────────────────────────────────────────────────────
Wide format encodes labels into column names (e.g. upf_packets{dir=rx}).
That works for plotting but breaks ML: models can't learn from a column name.
Long format gives each label its own column so the model sees `dir=rx` as a
feature value, not a column identity.

Usage
─────
  python promql/full_export.py                              # all metrics, last 14 days, 60s step
  python promql/full_export.py --days 7 --step 30s         # 7 days, finer resolution
  python promql/full_export.py --prefix upf_ pfcp_         # only these metric families
  python promql/full_export.py --out /tmp/train.csv        # custom output path
  python promql/full_export.py --prom-url http://x:9090    # remote Prometheus

Output columns
──────────────
  timestamp     Unix epoch seconds (float) — use as time feature or index
  datetime_utc  ISO-8601 string — human readable
  metric_name   e.g. "upf_packets_count"
  value         float — the raw scraped value
  <label_key>   one column per distinct label key found in the time range
                e.g. instance, job, iface, dir, quantile, ...
                Empty string when a series doesn't carry that label.

Memory usage
────────────
Rows are written to disk as they are fetched (chunk by chunk per metric),
so memory stays proportional to one chunk (~3h) not the full export.
"""

import argparse
import csv
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── Defaults ───────────────────────────────────────────────────────────────────

DEFAULT_PROM_URL    = "http://localhost:9090"
DEFAULT_DAYS        = 14.0
DEFAULT_STEP        = "60s"
DEFAULT_OUTPUT      = "prometheus_full_export.csv"
DEFAULT_CHUNK_HOURS = 3   # 3h per API call avoids Prometheus's default eval timeout


# ── Prometheus API helpers ─────────────────────────────────────────────────────

def get_metric_names(base_url: str, prefixes: list[str]) -> list[str]:
    """Return sorted list of all metric names, optionally filtered by prefix."""
    resp = requests.get(f"{base_url}/api/v1/label/__name__/values", timeout=30)
    resp.raise_for_status()
    names: list[str] = resp.json()["data"]
    if prefixes:
        names = [n for n in names if any(n.startswith(p) for p in prefixes)]
    return sorted(names)


def discover_label_keys(base_url: str, start_ts: float, end_ts: float, prefixes: list[str]) -> list[str]:
    """
    Call /api/v1/series to find every label key that exists in the time range.
    This is a metadata-only call — it returns label sets, not data points.
    Used to build the CSV header before any data is written.
    """
    if prefixes:
        regex = "|".join(f"{p}.*" for p in prefixes)
        match_expr = f'{{__name__=~"{regex}"}}'
    else:
        match_expr = '{__name__=~".+"}'

    resp = requests.get(
        f"{base_url}/api/v1/series",
        params={"match[]": match_expr, "start": start_ts, "end": end_ts},
        timeout=60,
    )
    resp.raise_for_status()

    all_keys: set[str] = set()
    for series in resp.json()["data"]:
        all_keys.update(k for k in series if k != "__name__")
    return sorted(all_keys)


def _query_range_chunk(base_url: str, metric: str, start: float, end: float, step: str) -> list[dict]:
    resp = requests.get(
        f"{base_url}/api/v1/query_range",
        params={"query": metric, "start": start, "end": end, "step": step},
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    if data["status"] != "success":
        return []
    return data["data"]["result"]


# ── Row iterator ───────────────────────────────────────────────────────────────

def iter_rows(
    base_url: str,
    metric: str,
    start_ts: float,
    end_ts: float,
    step: str,
    chunk_seconds: int,
    label_keys: list[str],
):
    """
    Yield one CSV row dict per (timestamp, label_set) data point.

    Queries in time chunks to avoid hitting Prometheus's per-query eval timeout
    (--query.timeout, default 2m). Each chunk fetches chunk_seconds of data.
    """
    chunk_start = start_ts
    while chunk_start < end_ts:
        chunk_end = min(chunk_start + chunk_seconds, end_ts)
        try:
            results = _query_range_chunk(base_url, metric, chunk_start, chunk_end, step)
        except requests.RequestException as exc:
            print(f"  WARNING: skipping chunk [{chunk_start:.0f}, {chunk_end:.0f}] for {metric}: {exc}",
                  file=sys.stderr)
            chunk_start = chunk_end
            continue

        for series in results:
            labels = {k: v for k, v in series["metric"].items() if k != "__name__"}
            for ts_str, val_str in series["values"]:
                row = {
                    "timestamp":    ts_str,
                    "datetime_utc": datetime.fromtimestamp(float(ts_str), tz=timezone.utc).isoformat(),
                    "metric_name":  metric,
                    "value":        val_str,
                }
                for key in label_keys:
                    row[key] = labels.get(key, "")
                yield row

        chunk_start = chunk_end


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="Export Prometheus TSDB → long-format CSV for ML training",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--prom-url",     default=DEFAULT_PROM_URL,
                   help=f"Prometheus base URL (default: {DEFAULT_PROM_URL})")
    p.add_argument("--days",         type=float, default=DEFAULT_DAYS,
                   help=f"Days of history to export (default: {DEFAULT_DAYS})")
    p.add_argument("--step",         default=DEFAULT_STEP,
                   help=f"Query resolution e.g. 15s, 60s, 5m (default: {DEFAULT_STEP})")
    p.add_argument("--prefix",       nargs="*", dest="prefixes", metavar="PREFIX",
                   help="Only export metrics starting with these prefixes (default: all metrics)")
    p.add_argument("--out",          default=DEFAULT_OUTPUT,
                   help=f"Output CSV path (default: {DEFAULT_OUTPUT})")
    p.add_argument("--chunk-hours",  type=int, default=DEFAULT_CHUNK_HOURS,
                   help=f"Hours per API request chunk (default: {DEFAULT_CHUNK_HOURS})")
    args = p.parse_args()

    end_ts   = time.time()
    start_ts = end_ts - (args.days * 86400)
    chunk_s  = args.chunk_hours * 3600

    fmt      = "%Y-%m-%d %H:%M UTC"
    t_start  = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime(fmt)
    t_end    = datetime.fromtimestamp(end_ts,   tz=timezone.utc).strftime(fmt)

    print(f"Prometheus : {args.prom_url}")
    print(f"Range      : {t_start}  →  {t_end}  ({args.days}d)")
    print(f"Step       : {args.step}   chunk: {args.chunk_hours}h")
    if args.prefixes:
        print(f"Prefix     : {args.prefixes}")

    # ── Discover schema ────────────────────────────────────────────────────────
    print("\nDiscovering metric names...")
    metrics = get_metric_names(args.prom_url, args.prefixes or [])
    if not metrics:
        sys.exit("No metrics found. Check --prefix or --prom-url.")
    print(f"  {len(metrics)} metrics")

    print("Discovering label keys from series metadata...")
    label_keys = discover_label_keys(args.prom_url, start_ts, end_ts, args.prefixes or [])
    print(f"  {len(label_keys)} label keys: {label_keys}")

    # ── Stream to CSV ──────────────────────────────────────────────────────────
    fieldnames = ["timestamp", "datetime_utc", "metric_name", "value"] + label_keys
    out_path   = Path(args.out)
    total_rows = 0

    print(f"\nWriting to {out_path} ...\n")
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore", restval="")
        writer.writeheader()

        for i, metric in enumerate(metrics, 1):
            count = 0
            for row in iter_rows(args.prom_url, metric, start_ts, end_ts,
                                  args.step, chunk_s, label_keys):
                writer.writerow(row)
                count += 1
            total_rows += count
            print(f"  [{i:3d}/{len(metrics)}] {metric:<50s} {count:>8,} rows")

    print(f"\nDone.  {total_rows:,} total rows  →  {out_path}")


if __name__ == "__main__":
    main()
