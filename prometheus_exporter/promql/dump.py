"""
Prometheus HTTP API → CSV.

Reads from /api/v1/label/__name__/values and /api/v1/query_range.
Values are raw cumulative (no diff applied) — every TSDB sample as-is.

Two modes
---------
snapshot (default)
  Fetch a fixed time window once and exit.

watch (--watch)
  Poll continuously, append only new rows.  The CSV is its own cursor:
  the last timestamp in it becomes the start of the next query, so no
  rows are ever duplicated or missed across restarts.

Usage
-----
  python promql/dump.py                          # last 1h snapshot
  python promql/dump.py --hours 6 --step 1m     # 6h snapshot
  python promql/dump.py --out my.csv            # named file
  python promql/dump.py --split                  # one CSV per metric
  python promql/dump.py --watch                  # live append, 30s poll
  python promql/dump.py --watch --interval 15   # live append, 15s poll
"""

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

# ── project root → shared data/ directory ─────────────────────────────────────
DATA_DIR = Path(__file__).parent.parent / "data"

PROM_URL        = "http://localhost:9090"
METRIC_PREFIXES = ("upf_", "pfcp_")
EXCLUDED_LABELS = {"__name__", "instance", "job"}


# ── Prometheus API helpers ─────────────────────────────────────────────────────

def get_metric_names(prom_url: str) -> list[str]:
    resp = requests.get(f"{prom_url}/api/v1/label/__name__/values", timeout=10)
    resp.raise_for_status()
    return [
        n for n in resp.json()["data"]
        if any(n.startswith(p) for p in METRIC_PREFIXES)
        and not n.endswith("_bucket")
    ]


def fetch_metric(prom_url: str, name: str,
                 start: datetime, end: datetime, step: str) -> pd.DataFrame:
    resp = requests.get(
        f"{prom_url}/api/v1/query_range",
        params={"query": name,
                "start": start.timestamp(),
                "end":   end.timestamp(),
                "step":  step},
        timeout=30,
    )
    resp.raise_for_status()
    results = resp.json()["data"]["result"]
    if not results:
        return pd.DataFrame()

    series = {}
    for r in results:
        labels    = {k: v for k, v in r["metric"].items() if k not in EXCLUDED_LABELS}
        label_str = "_".join(f"{k}={v}" for k, v in sorted(labels.items()))
        col       = name if not label_str else f"{name}{{{label_str}}}"
        vals      = pd.DataFrame(r["values"], columns=["ts", "value"])
        vals["ts"]    = pd.to_datetime(vals["ts"], unit="s", utc=True)
        vals["value"] = vals["value"].astype(float)
        series[col]   = vals.set_index("ts")["value"]

    return pd.DataFrame(series)


def fetch_all_metrics(prom_url: str, names: list[str],
                      start: datetime, end: datetime, step: str) -> pd.DataFrame:
    frames = [fetch_metric(prom_url, n, start, end, step) for n in names]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, axis=1).sort_index()


# ── CSV helpers ────────────────────────────────────────────────────────────────

def _last_ts(path: Path):
    if not path.exists() or path.stat().st_size == 0:
        return None
    try:
        df = pd.read_csv(path, index_col=0, parse_dates=True, usecols=[0])
        return df.index.max() if not df.empty else None
    except Exception:
        return None


def _append(df: pd.DataFrame, path: Path) -> int:
    write_header = not path.exists() or path.stat().st_size == 0
    df.to_csv(path, mode="a", header=write_header)
    return len(df)


def _step_seconds(step: str) -> int:
    if step.endswith("s"): return int(step[:-1])
    if step.endswith("m"): return int(step[:-1]) * 60
    if step.endswith("h"): return int(step[:-1]) * 3600
    return 30


# ── Modes ──────────────────────────────────────────────────────────────────────

def snapshot(args):
    end   = datetime.now(timezone.utc)
    start = end - timedelta(hours=args.hours)

    print(f"Prometheus : {args.prom_url}")
    print(f"Window     : {start:%H:%M UTC} → {end:%H:%M UTC}  step={args.step}\n")

    names = get_metric_names(args.prom_url)
    if not names:
        print("No upf_*/pfcp_* metrics. Is Prometheus scraping :8080?")
        sys.exit(1)

    DATA_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    if args.split:
        for name in names:
            df = fetch_metric(args.prom_url, name, start, end, args.step)
            if df.empty:
                print(f"  {name}: no data")
                continue
            out = DATA_DIR / f"prom_dump_{name}_{ts}.csv"
            df.to_csv(out)
            print(f"  {name}: {len(df)} rows → {out.name}")
    else:
        df = fetch_all_metrics(args.prom_url, names, start, end, args.step)
        if df.empty:
            print("No data returned.")
            sys.exit(1)
        out = Path(args.out) if args.out else DATA_DIR / f"prom_dump_{ts}.csv"
        df.to_csv(out)
        print(f"Wrote {len(df)} rows × {len(df.columns)} columns → {out}")


def watch(args):
    DATA_DIR.mkdir(exist_ok=True)
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(args.out) if args.out else DATA_DIR / f"prom_live_{ts}.csv"

    print(f"Prometheus : {args.prom_url}")
    print(f"Output     : {out}  (appending every {args.interval}s)")
    print("Press Ctrl-C to stop.\n")

    names = get_metric_names(args.prom_url)
    if not names:
        print("No upf_*/pfcp_* metrics. Is Prometheus scraping :8080?")
        sys.exit(1)
    print(f"Tracking {len(names)} metric series\n")

    step_sec = _step_seconds(args.step)
    poll = 0

    try:
        while True:
            poll += 1
            end    = datetime.now(timezone.utc)
            last   = _last_ts(out)
            start  = last + timedelta(seconds=step_sec) if last else end - timedelta(hours=args.hours)
            now_s  = end.strftime("%H:%M:%S")

            if start >= end:
                print(f"  [{now_s}] #{poll}: up to date")
            else:
                df = fetch_all_metrics(args.prom_url, names, start, end, args.step)
                if df.empty:
                    print(f"  [{now_s}] #{poll}: no new rows")
                else:
                    n = _append(df, out)
                    print(f"  [{now_s}] #{poll}: +{n} rows → {out.name}")

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print(f"\nStopped after {poll} polls. Data in: {out}")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Prometheus API → CSV (snapshot or watch)")
    p.add_argument("--prom-url",  default=PROM_URL)
    p.add_argument("--hours",     type=float, default=1.0,
                   help="History window (default: 1h)")
    p.add_argument("--step",      default="30s",
                   help="Resolution e.g. 15s / 1m (default: 30s)")
    p.add_argument("--out",       default="",
                   help="Output CSV path (default: auto-named in data/)")
    p.add_argument("--split",     action="store_true",
                   help="[Snapshot] one CSV per metric")
    p.add_argument("--watch",     action="store_true",
                   help="Poll continuously, append new rows")
    p.add_argument("--interval",  type=int, default=30,
                   help="[Watch] seconds between polls (default: 30)")
    args = p.parse_args()

    if args.watch:
        watch(args)
    else:
        snapshot(args)


if __name__ == "__main__":
    main()
