"""
promtool tsdb dump → CSV live watcher.

Reads Prometheus TSDB blocks and WAL directly via the native `promtool`
binary inside the container.  No HTTP API calls for data extraction.

Change detection
----------------
The watcher checks the mtime of the active WAL segment file:
  /prometheus/wal/00000000000000000001   (Prometheus appends here every scrape)
When the mtime advances, new samples are in the WAL → dump triggered.

Data extraction
---------------
  docker exec prom promtool tsdb dump /prometheus --min-time=<last_ms>

Only rows newer than the last exported timestamp are fetched (incremental).
Output is parsed from promtool's OpenMetrics-like format:
  {__name__="pfcp_sessions", instance="...", job="..."} 50000 1749453600000
  ─────────────── labels ──────────────────────────────  value  epoch_ms

Comparison with promql/dump.py --watch
---------------------------------------
  promql path    : HTTP → Prometheus query engine → Python
  promtool path  : docker exec → TSDB blocks + WAL → Python

Effective latency is identical (~15s = one scrape interval).
Advantage of promtool: works even if the HTTP API is overloaded or stopped.

Usage
-----
  python promtool/watcher.py                       # 15s poll, auto-named CSV
  python promtool/watcher.py --interval 30         # 30s poll
  python promtool/watcher.py --container my-prom   # custom container name
  python promtool/watcher.py --out live.csv        # named output
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

# ── Defaults ───────────────────────────────────────────────────────────────────

PROM_CONTAINER  = "upf-prom"    # docker container name  (docker ps)
PROM_DATA_DIR   = "/prometheus" # --storage.tsdb.path inside container

# ── Include filters (OR logic — a metric passes if ANY include filter matches) ──
#
# Keep metrics whose __name__ STARTS WITH any of these.  Empty = no prefix filter.
#   METRIC_PREFIXES = ("pfcp_", "upf_", "bess_")
METRIC_PREFIXES: tuple = ()

# Keep metrics whose __name__ CONTAINS any of these words (substring match).
# Empty = no word filter.
#   METRIC_CONTAINS = ("sessions", "drop", "throughput")
METRIC_CONTAINS: tuple = ()

# ── Exclude filters (applied AFTER include — take precedence) ────────────────
#
# Drop metrics whose __name__ STARTS WITH any of these.  Empty = drop nothing.
#   EXCLUDE_PREFIXES = ("go_", "process_", "promhttp_")
EXCLUDE_PREFIXES: tuple = ()

# Drop metrics whose __name__ CONTAINS any of these words (substring match).
# Empty = drop nothing.
#   EXCLUDE_CONTAINS = ("debug", "internal", "test")
EXCLUDE_CONTAINS: tuple = ()

# ── Label filter ─────────────────────────────────────────────────────────────
#
# Strip these label keys from column names to reduce cardinality.
#   EXCLUDED_LABELS = {"instance", "job", "pod"}
EXCLUDED_LABELS: set = set()

DATA_DIR = Path(__file__).parent.parent / "data"

# promtool output format:
#   {key="val", ...} float_value epoch_ms
LINE_RE = re.compile(r'^\{(.+?)\}\s+(\S+)\s+(\d+)$')


# ── WAL change detection ───────────────────────────────────────────────────────

def _current_wal_segment(container: str, data_dir: str) -> str:
    result = subprocess.run(
        ["docker", "exec", container, "sh", "-c",
         f"ls -1 {data_dir}/wal/ 2>/dev/null | grep -v checkpoint | sort | tail -1"],
        capture_output=True, text=True, timeout=10,
    )
    seg = result.stdout.strip()
    return f"{data_dir}/wal/{seg}" if seg else f"{data_dir}/wal"


def get_wal_mtime(container: str, data_dir: str) -> float:
    """Return mtime (epoch seconds) of the active WAL segment file.

    Prometheus appends to this file on every scrape (~15s).
    A mtime change means new samples are available.
    """
    seg = _current_wal_segment(container, data_dir)
    result = subprocess.run(
        ["docker", "exec", container, "stat", "-c", "%Y", seg],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode == 0 and result.stdout.strip():
        try:
            return float(result.stdout.strip())
        except ValueError:
            pass
    return time.time()  # fallback: treat as always changed


# ── Data extraction ────────────────────────────────────────────────────────────

def _supports_sandbox_flag(container: str) -> bool:
    """Return True if this promtool build understands --sandbox-dir-root (3.x+)."""
    r = subprocess.run(
        ["docker", "exec", container, "promtool", "tsdb", "dump", "--help"],
        capture_output=True, text=True, timeout=10,
    )
    return "--sandbox-dir-root" in (r.stdout + r.stderr)


def run_dump(container: str, data_dir: str, min_ts_ms: int,
             use_sandbox: bool = True) -> str:
    if use_sandbox:
        # promtool 3.x: sandbox must be on the same filesystem as data_dir so
        # promtool can hardlink chunk files instead of copying them.
        # Using data_dir itself as the root keeps everything on one device.
        result = subprocess.run(
            ["docker", "exec", container, "promtool", "tsdb", "dump",
             "--sandbox-dir-root", data_dir,
             "--min-time", str(min_ts_ms), data_dir],
            capture_output=True, text=True, timeout=120,
        )
    else:
        # promtool 2.x: manually copy TSDB to /tmp before reading.
        # Replaying the live WAL in-place is unsafe while Prometheus writes to it;
        # reading from a snapshot avoids torn reads and WAL errors.
        sandbox = f"/tmp/prom_snap_{int(time.time())}"
        try:
            cp = subprocess.run(
                ["docker", "exec", container, "cp", "-r", data_dir, sandbox],
                capture_output=True, text=True, timeout=120,
            )
            if cp.returncode != 0:
                print(f"  [WARN] snapshot copy failed: {cp.stderr.strip()[:200]}")
                # Best-effort fallback: read live (may emit WAL warnings)
                result = subprocess.run(
                    ["docker", "exec", container, "promtool", "tsdb", "dump",
                     "--min-time", str(min_ts_ms), data_dir],
                    capture_output=True, text=True, timeout=120,
                )
            else:
                result = subprocess.run(
                    ["docker", "exec", container, "promtool", "tsdb", "dump",
                     "--min-time", str(min_ts_ms), sandbox],
                    capture_output=True, text=True, timeout=120,
                )
        finally:
            subprocess.run(
                ["docker", "exec", container, "rm", "-rf", sandbox],
                capture_output=True, text=True, timeout=30,
            )

    if result.returncode != 0:
        print(f"  [WARN] promtool exit {result.returncode}: "
              f"{result.stderr.strip()[:300]}")
    return result.stdout


def parse_dump(output: str, min_ts_ms: int) -> list[dict]:
    """Parse promtool output → list of {ts_ms, col, value} dicts."""
    samples = []
    for line in output.splitlines():
        m = LINE_RE.match(line.strip())
        if not m:
            continue
        labels_str, value_str, ts_str = m.groups()

        ts_ms = int(ts_str)
        if ts_ms <= min_ts_ms:
            continue

        labels = dict(re.findall(r'(\w+)="([^"]*)"', labels_str))
        name   = labels.get("__name__", "")

        # Include check: pass only when at least one include filter matches.
        # If both are empty the metric passes unconditionally.
        if METRIC_PREFIXES or METRIC_CONTAINS:
            if not (any(name.startswith(p) for p in METRIC_PREFIXES) or
                    any(w in name            for w in METRIC_CONTAINS)):
                continue

        # Exclude check: drop regardless of what the include filter said.
        if EXCLUDE_PREFIXES and any(name.startswith(p) for p in EXCLUDE_PREFIXES):
            continue
        if EXCLUDE_CONTAINS and any(w in name for w in EXCLUDE_CONTAINS):
            continue

        if name.endswith("_bucket"):
            continue

        try:
            value = float(value_str)
        except ValueError:
            continue

        useful    = {k: v for k, v in labels.items()
                     if k != "__name__" and k not in EXCLUDED_LABELS}
        label_str = "_".join(f"{k}={v}" for k, v in sorted(useful.items()))
        col       = name if not label_str else f"{name}{{{label_str}}}"

        samples.append({"ts_ms": ts_ms, "col": col, "value": value})

    return samples


def pivot(samples: list[dict]) -> pd.DataFrame:
    """Long-format sample list → wide DataFrame with UTC timestamps."""
    if not samples:
        return pd.DataFrame()
    df   = pd.DataFrame(samples)
    wide = df.pivot_table(index="ts_ms", columns="col",
                          values="value", aggfunc="last")
    wide.index = pd.to_datetime(wide.index, unit="ms", utc=True)
    wide.index.name = "timestamp"
    return wide


# ── CSV helpers ────────────────────────────────────────────────────────────────

def last_ts_ms(path: Path) -> int:
    """Return last timestamp in the CSV as epoch milliseconds, or 0."""
    if not path.exists() or path.stat().st_size == 0:
        return 0
    try:
        df = pd.read_csv(path, index_col=0, usecols=[0])
        if df.index.empty:
            return 0
        # parse_dates=True no longer auto-parses index in pandas 2.x;
        # explicit conversion handles both tz-aware and tz-naive strings.
        ts = pd.to_datetime(df.index, utc=True).max()
        return int(ts.timestamp() * 1000)
    except Exception:
        return 0


def append(df: pd.DataFrame, path: Path) -> int:
    write_header = not path.exists() or path.stat().st_size == 0
    df.to_csv(path, mode="a", header=write_header)
    return len(df)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="promtool tsdb dump → CSV live watcher")
    p.add_argument("--container", default=PROM_CONTAINER,
                   help=f"Prometheus container name (default: {PROM_CONTAINER})")
    p.add_argument("--data-dir",  default=PROM_DATA_DIR,
                   help=f"TSDB path inside container (default: {PROM_DATA_DIR})")
    p.add_argument("--interval",  type=int, default=15,
                   help="Poll interval in seconds (default: 15)")
    p.add_argument("--hours",     type=float, default=1.0,
                   help="Seed window on first run (default: 1h)")
    p.add_argument("--out",       default="",
                   help="Output CSV path (default: auto-named in data/)")
    args = p.parse_args()

    # Verify promtool is available
    check = subprocess.run(
        ["docker", "exec", args.container, "promtool", "--version"],
        capture_output=True, text=True, timeout=10,
    )
    if check.returncode != 0:
        running = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True,
        ).stdout.strip()
        print(f"ERROR: promtool not found in container '{args.container}'.")
        print(f"       Running containers: {running}")
        sys.exit(1)

    use_sandbox = _supports_sandbox_flag(args.container)

    DATA_DIR.mkdir(exist_ok=True)
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(args.out) if args.out else DATA_DIR / f"promtool_live_{ts}.csv"

    print(f"Container  : {args.container}  ({args.data_dir})")
    print(f"Output     : {out}")
    print(f"Interval   : {args.interval}s (WAL-mtime triggered)")
    print(f"Sandbox    : {'yes (--sandbox-dir-root)' if use_sandbox else 'no (promtool <3.x, stop Prometheus first if TSDB is locked)'}")
    print("Press Ctrl-C to stop.\n")

    last_wal = 0.0
    poll     = 0

    try:
        while True:
            poll    += 1
            now_str  = datetime.now().strftime("%H:%M:%S")
            wal_mt   = get_wal_mtime(args.container, args.data_dir)

            if wal_mt <= last_wal:
                print(f"  [{now_str}] #{poll}: WAL unchanged, waiting…")
                time.sleep(args.interval)
                continue

            last_wal = wal_mt
            l_ms     = last_ts_ms(out)

            if l_ms == 0:
                seed = datetime.now(timezone.utc) - timedelta(hours=args.hours)
                l_ms = int(seed.timestamp() * 1000)
                print(f"  [{now_str}] #{poll}: first run, seeding {args.hours}h back")
            else:
                ts_dt = datetime.fromtimestamp(l_ms / 1000, tz=timezone.utc)
                print(f"  [{now_str}] #{poll}: WAL updated, dumping since {ts_dt:%H:%M:%S UTC}")

            raw     = run_dump(args.container, args.data_dir, l_ms, use_sandbox)
            samples = parse_dump(raw, l_ms)

            if not samples:
                print(f"  [{now_str}] #{poll}: no new rows")
            else:
                df = pivot(samples)
                n  = append(df, out)
                print(f"  [{now_str}] #{poll}: +{n} rows, "
                      f"{len(df.columns)} cols → {out.name}")

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print(f"\nStopped after {poll} polls. Data in: {out}")


if __name__ == "__main__":
    main()
