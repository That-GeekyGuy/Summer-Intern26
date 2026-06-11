"""
Prometheus → ML-ready CSV.

Discovers every upf_*/pfcp_* metric dynamically via /api/v1/metadata,
applies per-type transformations (counters → per-step delta, histograms
expanded to _count/_sum), and writes a wide feature DataFrame to CSV.

Unlike dump.py (raw cumulative values), this produces features suitable for
direct ingestion into ML models without further pre-processing.

Usage
-----
  python promql/ml_export.py                      # last 1h, 30s step
  python promql/ml_export.py --hours 24 --step 1m # 24h training slice
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow `from fetch import ...` regardless of working directory
sys.path.insert(0, str(Path(__file__).parent))

from fetch import fetch_all        # dynamic discovery + counter diff
import csv_writer                  # write / append helpers

DATA_DIR = Path(__file__).parent.parent / "data"


def main():
    p = argparse.ArgumentParser(description="Prometheus → ML-ready CSV")
    p.add_argument("--prom-url", default="http://localhost:9090")
    p.add_argument("--hours",    type=float, default=1.0,
                   help="Hours of history to fetch (default: 1)")
    p.add_argument("--step",     default="30s",
                   help="Query resolution e.g. 15s / 1m (default: 30s)")
    args = p.parse_args()

    end   = datetime.now(timezone.utc)
    start = end - timedelta(hours=args.hours)

    print(f"Fetching {args.hours}h of metrics  (step={args.step})…")
    df = fetch_all(args.prom_url, start, end, args.step)

    if df.empty:
        print("No data returned. Is Prometheus scraping the UPF?")
        return

    print(f"\nShape   : {len(df)} rows × {len(df.columns)} columns")
    print(f"Window  : {df.index[0]}  →  {df.index[-1]}")
    print(f"Columns : {list(df.columns)}\n")
    print(df.describe().to_string(), "\n")

    csv_writer.write(df, DATA_DIR, prefix="ml_export")


if __name__ == "__main__":
    main()
