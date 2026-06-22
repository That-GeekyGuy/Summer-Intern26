#!/usr/bin/env python3
"""
full_export.py
──────────────
Export the full Prometheus TSDB to a long-format CSV for ML training.

Queries the Prometheus HTTP API from *inside* the container via
`docker exec <container> curl`, mirroring the same pattern that
watcher.py uses for `docker exec <container> promtool`.
No host-side port binding required — works even if :9090 is not exposed.

Each row = one (timestamp, metric_name, value, ...label columns) data point.
Labels become individual columns — categorical features ready for model ingestion.

Usage
─────
  python promql/full_export.py                              # all metrics, last 14 days, 60s step
  python promql/full_export.py --container my-prom         # custom container name
  python promql/full_export.py --days 7 --step 30s         # 7 days, finer resolution
  python promql/full_export.py --prefix upf_ pfcp_         # only these metric families
  python promql/full_export.py --exclude-prefix go_ process_
  python promql/full_export.py --out /tmp/train.csv        # custom output path

Output columns
──────────────
  timestamp     Unix epoch seconds (float)
  datetime_utc  ISO-8601 string
  metric_name   e.g. "upf_packets_count"
  value         float — the raw scraped value
  <label_key>   one column per distinct label key found in the time range
                Empty string when a series doesn't carry that label.

Memory usage
────────────
Rows are streamed to disk chunk by chunk — memory stays proportional
to one time chunk (~3h), not the full export.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

# ── Defaults ───────────────────────────────────────────────────────────────────

PROM_CONTAINER  = "upf-prom"
PROM_URL        = "http://localhost:9090"   # URL as seen from inside the container
DEFAULT_DAYS    = 14.0
DEFAULT_STEP    = "60s"
DEFAULT_OUTPUT  = "prometheus_full_export.csv"
CHUNK_HOURS     = 3    # hours per query_range call — avoids eval timeout

DATA_DIR = Path(__file__).parent.parent / "data"


# ── docker exec curl helper ────────────────────────────────────────────────────

def _curl(container: str, prom_url: str, path: str, params: dict | None = None) -> dict:
    """Run curl inside the container and return the parsed JSON response."""
    url = f"{prom_url}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    result = subprocess.run(
        ["docker", "exec", container, "curl", "-s", url],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"curl exit {result.returncode}: {result.stderr.strip()[:200]}"
        )
    return json.loads(result.stdout)


# ── Prometheus API calls ───────────────────────────────────────────────────────

def get_metric_names(container: str, prom_url: str, prefixes: list[str]) -> list[str]:
    data = _curl(container, prom_url, "/api/v1/label/__name__/values")
    names: list[str] = data["data"]
    if prefixes:
        names = [n for n in names if any(n.startswith(p) for p in prefixes)]
    return sorted(names)


def discover_label_keys(
    container: str, prom_url: str,
    start_ts: float, end_ts: float,
    prefixes: list[str],
    exclude_prefixes: list[str],
) -> list[str]:
    """
    Call /api/v1/series (metadata only — no data points) to find every
    label key that exists in the time range.  Used to build the CSV header.
    """
    if prefixes:
        regex = "|".join(f"{p}.*" for p in prefixes)
        match_expr = f'{{__name__=~"{regex}"}}'
    else:
        match_expr = '{__name__=~".+"}'

    data = _curl(container, prom_url, "/api/v1/series", {
        "match[]": match_expr,
        "start":   start_ts,
        "end":     end_ts,
    })

    all_keys: set[str] = set()
    for series in data["data"]:
        name = series.get("__name__", "")
        if exclude_prefixes and any(name.startswith(p) for p in exclude_prefixes):
            continue
        all_keys.update(k for k in series if k != "__name__")
    return sorted(all_keys)


def _query_range_chunk(
    container: str, prom_url: str,
    metric: str, start: float, end: float, step: str,
) -> list[dict]:
    data = _curl(container, prom_url, "/api/v1/query_range", {
        "query": metric,
        "start": start,
        "end":   end,
        "step":  step,
    })
    if data.get("status") != "success":
        return []
    return data["data"]["result"]


# ── Row iterator ───────────────────────────────────────────────────────────────

def iter_rows(
    container: str,
    prom_url: str,
    metric: str,
    start_ts: float,
    end_ts: float,
    step: str,
    chunk_seconds: int,
    label_keys: list[str],
):
    """Yield one CSV row dict per (timestamp, label_set) data point, chunked."""
    chunk_start = start_ts
    while chunk_start < end_ts:
        chunk_end = min(chunk_start + chunk_seconds, end_ts)
        try:
            results = _query_range_chunk(container, prom_url, metric, chunk_start, chunk_end, step)
        except Exception as exc:
            print(f"  WARNING: skipping chunk for {metric}: {exc}", file=sys.stderr)
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
    p.add_argument("--container",      default=PROM_CONTAINER,
                   help=f"Docker container running Prometheus (default: {PROM_CONTAINER})")
    p.add_argument("--prom-url",       default=PROM_URL,
                   help=f"Prometheus URL as seen inside the container (default: {PROM_URL})")
    p.add_argument("--days",           type=float, default=DEFAULT_DAYS,
                   help=f"Days of history to export (default: {DEFAULT_DAYS})")
    p.add_argument("--step",           default=DEFAULT_STEP,
                   help=f"Query resolution e.g. 15s, 60s, 5m (default: {DEFAULT_STEP})")
    p.add_argument("--prefix",         nargs="*", dest="prefixes", metavar="PREFIX",
                   help="Only export metrics starting with these prefixes (default: all)")
    p.add_argument("--exclude-prefix", nargs="*", dest="exclude_prefixes", metavar="PREFIX",
                   help="Drop metrics starting with these prefixes")
    p.add_argument("--exclude-labels", nargs="*", dest="exclude_labels", metavar="LABEL",
                   help="Label keys to omit from CSV columns (default: none — all labels kept)")
    p.add_argument("--out",            default="",
                   help=f"Output CSV path (default: auto-named in data/)")
    p.add_argument("--chunk-hours",    type=int, default=CHUNK_HOURS,
                   help=f"Hours per API chunk (default: {CHUNK_HOURS})")
    args = p.parse_args()

    prefixes         = args.prefixes or []
    exclude_prefixes = args.exclude_prefixes or []
    exclude_labels   = set(args.exclude_labels or [])
    chunk_s          = args.chunk_hours * 3600

    # ── Verify container + Prometheus are reachable ────────────────────────────
    check = subprocess.run(
        ["docker", "exec", args.container,
         "curl", "-sf", f"{args.prom_url}/-/ready"],
        capture_output=True, text=True, timeout=10,
    )
    if check.returncode != 0:
        running = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True,
        ).stdout.strip()
        print(f"ERROR: cannot reach Prometheus in container '{args.container}'.")
        print(f"       Running containers: {running}")
        sys.exit(1)

    end_ts   = time.time()
    start_ts = end_ts - (args.days * 86400)
    fmt      = "%Y-%m-%d %H:%M UTC"

    DATA_DIR.mkdir(exist_ok=True)
    ts_str   = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = Path(args.out) if args.out else DATA_DIR / f"prometheus_full_export_{ts_str}.csv"

    print(f"Container  : {args.container}  ({args.prom_url})")
    print(f"Range      : {datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime(fmt)}"
          f"  →  {datetime.fromtimestamp(end_ts, tz=timezone.utc).strftime(fmt)}  ({args.days}d)")
    print(f"Step       : {args.step}   chunk: {args.chunk_hours}h")
    if prefixes:         print(f"Include    : {prefixes}")
    if exclude_prefixes: print(f"Exclude    : {exclude_prefixes}")
    if exclude_labels:   print(f"Drop labels: {sorted(exclude_labels)}")

    # ── Discover schema ────────────────────────────────────────────────────────
    print("\nDiscovering metric names...")
    metrics = get_metric_names(args.container, args.prom_url, prefixes)
    if exclude_prefixes:
        metrics = [m for m in metrics if not any(m.startswith(p) for p in exclude_prefixes)]
    if not metrics:
        sys.exit("No metrics found. Check --prefix or --container.")
    print(f"  {len(metrics)} metrics")

    print("Discovering label keys from series metadata...")
    label_keys = discover_label_keys(
        args.container, args.prom_url,
        start_ts, end_ts,
        prefixes, exclude_prefixes,
    )
    label_keys = [k for k in label_keys if k not in exclude_labels]
    print(f"  {len(label_keys)} label keys: {label_keys}")

    # ── Stream to CSV ──────────────────────────────────────────────────────────
    fieldnames = ["timestamp", "datetime_utc", "metric_name", "value"] + label_keys
    total_rows = 0

    print(f"\nWriting to {out_path} ...\n")
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore", restval="")
        writer.writeheader()

        for i, metric in enumerate(metrics, 1):
            count = 0
            for row in iter_rows(args.container, args.prom_url, metric,
                                  start_ts, end_ts, args.step, chunk_s, label_keys):
                writer.writerow(row)
                count += 1
            total_rows += count
            print(f"  [{i:3d}/{len(metrics)}] {metric:<50s} {count:>8,} rows")

    print(f"\nDone.  {total_rows:,} total rows  →  {out_path}")


if __name__ == "__main__":
    main()
