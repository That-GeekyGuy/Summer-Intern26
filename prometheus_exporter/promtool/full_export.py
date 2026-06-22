"""
promtool tsdb dump → long-format CSV for ML training.

Architecture mirrors watcher.py exactly:
  • WAL mtime change detection — triggers only when new scrapes land
  • docker exec promtool tsdb dump — reads TSDB blocks + WAL directly,
    no HTTP API, works even when the query engine is overloaded
  • Incremental export — --min-time cursor means no rows are ever duplicated
  • Sandbox auto-detection — uses --sandbox-dir-root on promtool 3.x,
    falls back to /tmp copy on 2.x
  • Same four include/exclude filter constants at the top of the file
  • Same CLI flags: --container, --data-dir, --interval, --hours, --out
    plus filter overrides: --include-prefix, --include-contains,
    --exclude-prefix, --exclude-contains, --exclude-labels

Key difference from watcher.py
-------------------------------
  watcher.py        wide format — labels baked into column names
                    e.g. upf_packets_count{dir=rx,iface=Access}
  full_export.py    long format — every label is its own column
                    metric_name | value | dir | iface | instance | job | …

Every label that appears in the TSDB is captured as a column.
The schema is discovered from the first dump and then fixed for the session
so the CSV header is consistent across all appended batches.
Missing labels for a given row are written as empty strings.

Usage
-----
  python promtool/full_export.py                         # 15s poll, auto-named CSV
  python promtool/full_export.py --interval 30           # 30s poll
  python promtool/full_export.py --container my-prom     # custom container name
  python promtool/full_export.py --hours 336             # seed 14 days on first run
  python promtool/full_export.py --out /tmp/train.csv    # named output
  python promtool/full_export.py --include-prefix upf_ pfcp_
  python promtool/full_export.py --exclude-prefix go_ process_
  python promtool/full_export.py --exclude-labels instance job
"""

from __future__ import annotations

import argparse
import csv
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

# ── Defaults ───────────────────────────────────────────────────────────────────

PROM_CONTAINER = "upf-prom"     # docker container name  (docker ps)
PROM_DATA_DIR  = "/prometheus"  # --storage.tsdb.path inside the container

# ── Include filters (OR logic — metric passes if ANY include filter matches) ───
#
# Keep metrics whose __name__ STARTS WITH any of these.  Empty = no prefix filter.
#   METRIC_PREFIXES = ("pfcp_", "upf_", "bess_")
METRIC_PREFIXES: tuple = ()

# Keep metrics whose __name__ CONTAINS any of these words (substring match).
# Empty = no word filter.  Both empty → all metrics pass.
#   METRIC_CONTAINS = ("sessions", "drop", "throughput")
METRIC_CONTAINS: tuple = ()

# ── Exclude filters (applied AFTER include — take precedence) ─────────────────
#
# Drop metrics whose __name__ STARTS WITH any of these.  Empty = drop nothing.
#   EXCLUDE_PREFIXES = ("go_", "process_", "promhttp_")
EXCLUDE_PREFIXES: tuple = ()

# Drop metrics whose __name__ CONTAINS any of these words.  Empty = drop nothing.
#   EXCLUDE_CONTAINS = ("debug", "internal")
EXCLUDE_CONTAINS: tuple = ()

DATA_DIR = Path(__file__).parent.parent / "data"

# promtool output line format:
#   {key="val", key2="val2", ...} float_value epoch_ms
LINE_RE = re.compile(r'^\{(.+?)\}\s+(\S+)\s+(\d+)$')

# Columns that are always present; label keys come after these.
BASE_FIELDS = ["timestamp_ms", "datetime_utc", "metric_name", "value"]


# ── WAL change detection (identical to watcher.py) ────────────────────────────

def _current_wal_segment(container: str, data_dir: str) -> str:
    result = subprocess.run(
        ["docker", "exec", container, "sh", "-c",
         f"ls -1 {data_dir}/wal/ 2>/dev/null | grep -v checkpoint | sort | tail -1"],
        capture_output=True, text=True, timeout=10,
    )
    seg = result.stdout.strip()
    return f"{data_dir}/wal/{seg}" if seg else f"{data_dir}/wal"


def get_wal_mtime(container: str, data_dir: str) -> float:
    """Return mtime (epoch seconds) of the active WAL segment.

    Prometheus appends to this file every scrape (~15s).  A mtime change
    means new samples are available — same trigger as watcher.py.
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


# ── promtool helpers (identical to watcher.py) ────────────────────────────────

def _supports_sandbox_flag(container: str) -> bool:
    """Return True if this promtool build understands --sandbox-dir-root (3.x+)."""
    r = subprocess.run(
        ["docker", "exec", container, "promtool", "tsdb", "dump", "--help"],
        capture_output=True, text=True, timeout=10,
    )
    return "--sandbox-dir-root" in (r.stdout + r.stderr)


def run_dump(container: str, data_dir: str, min_ts_ms: int, use_sandbox: bool) -> str:
    """Run promtool tsdb dump and return stdout."""
    if use_sandbox:
        result = subprocess.run(
            ["docker", "exec", container, "promtool", "tsdb", "dump",
             "--sandbox-dir-root", data_dir,
             "--min-time", str(min_ts_ms), data_dir],
            capture_output=True, text=True, timeout=120,
        )
    else:
        # promtool 2.x: copy TSDB to /tmp to avoid reading a live WAL in-place.
        sandbox = f"/tmp/prom_snap_{int(time.time())}"
        try:
            cp = subprocess.run(
                ["docker", "exec", container, "cp", "-r", data_dir, sandbox],
                capture_output=True, text=True, timeout=120,
            )
            if cp.returncode != 0:
                print(f"  [WARN] snapshot copy failed: {cp.stderr.strip()[:200]}")
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


# ── Parsing ────────────────────────────────────────────────────────────────────

def parse_dump(output: str, min_ts_ms: int, exclude_labels: set[str]) -> list[dict]:
    """Parse promtool output lines into flat row dicts.

    Each row has BASE_FIELDS + one key per label found in that sample.
    ALL label keys are preserved as individual dict keys — no encoding into
    column names.  exclude_labels lets callers drop high-noise keys (e.g.
    instance, job) that are constant across all series.
    """
    rows = []
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
        # Both tuples empty → metric passes unconditionally.
        if METRIC_PREFIXES or METRIC_CONTAINS:
            if not (any(name.startswith(p) for p in METRIC_PREFIXES) or
                    any(w in name           for w in METRIC_CONTAINS)):
                continue

        # Exclude check: drop regardless of what the include filter said.
        if EXCLUDE_PREFIXES and any(name.startswith(p) for p in EXCLUDE_PREFIXES):
            continue
        if EXCLUDE_CONTAINS and any(w in name for w in EXCLUDE_CONTAINS):
            continue

        # Histogram buckets carry no useful scalar signal for tabular ML.
        if name.endswith("_bucket"):
            continue

        try:
            value = float(value_str)
        except ValueError:
            continue

        row: dict = {
            "timestamp_ms": ts_ms,
            "datetime_utc": datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat(),
            "metric_name":  name,
            "value":        value,
        }
        for k, v in labels.items():
            if k == "__name__":
                continue           # captured as metric_name
            if k in exclude_labels:
                continue
            row[k] = v

        rows.append(row)
    return rows


# ── Schema helpers ─────────────────────────────────────────────────────────────

def discover_label_keys(rows: list[dict]) -> list[str]:
    """Return sorted list of all label keys found across a batch of rows."""
    fixed = set(BASE_FIELDS)
    keys: set[str] = set()
    for row in rows:
        keys.update(k for k in row if k not in fixed)
    return sorted(keys)


def read_schema(path: Path) -> list[str] | None:
    """Read CSV header from an existing file to restore the schema on restart."""
    if not path.exists() or path.stat().st_size == 0:
        return None
    try:
        with path.open("r", newline="", encoding="utf-8") as f:
            return next(csv.reader(f))
    except (StopIteration, csv.Error):
        return None


# ── CSV helpers ────────────────────────────────────────────────────────────────

def write_rows(rows: list[dict], path: Path, fieldnames: list[str]) -> int:
    """Append rows to CSV.  Header is written only when the file is new/empty."""
    write_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore", restval="")
        if write_header:
            writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def last_ts_ms(path: Path) -> int:
    """Return the maximum timestamp_ms in the CSV as an integer, or 0."""
    if not path.exists() or path.stat().st_size == 0:
        return 0
    try:
        df = pd.read_csv(path, usecols=["timestamp_ms"])
        return int(df["timestamp_ms"].max()) if not df.empty else 0
    except Exception:
        return 0


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="promtool tsdb dump → long-format CSV for ML training",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--container",        default=PROM_CONTAINER,
                   help=f"Prometheus container name (default: {PROM_CONTAINER})")
    p.add_argument("--data-dir",         default=PROM_DATA_DIR,
                   help=f"TSDB path inside container (default: {PROM_DATA_DIR})")
    p.add_argument("--interval",         type=int, default=15,
                   help="Poll interval in seconds (default: 15)")
    p.add_argument("--hours",            type=float, default=1.0,
                   help="Seed window on first run (default: 1h). Use 336 for 14 days.")
    p.add_argument("--out",              default="",
                   help="Output CSV path (default: auto-named in data/)")
    # Filter overrides — mirror the module-level constants as CLI flags
    p.add_argument("--include-prefix",   nargs="*", dest="include_prefix",  metavar="PREFIX",
                   help="Only include metrics starting with these (overrides METRIC_PREFIXES)")
    p.add_argument("--include-contains", nargs="*", dest="include_contains", metavar="WORD",
                   help="Only include metrics containing these substrings (overrides METRIC_CONTAINS)")
    p.add_argument("--exclude-prefix",   nargs="*", dest="exclude_prefix",  metavar="PREFIX",
                   help="Drop metrics starting with these (overrides EXCLUDE_PREFIXES)")
    p.add_argument("--exclude-contains", nargs="*", dest="exclude_contains", metavar="WORD",
                   help="Drop metrics containing these substrings (overrides EXCLUDE_CONTAINS)")
    p.add_argument("--exclude-labels",   nargs="*", dest="exclude_labels",  metavar="LABEL",
                   help="Label keys to omit from CSV columns (default: none — all labels kept)")
    args = p.parse_args()

    # Override module-level filter constants with CLI flags when provided.
    global METRIC_PREFIXES, METRIC_CONTAINS, EXCLUDE_PREFIXES, EXCLUDE_CONTAINS
    if args.include_prefix:   METRIC_PREFIXES  = tuple(args.include_prefix)
    if args.include_contains: METRIC_CONTAINS  = tuple(args.include_contains)
    if args.exclude_prefix:   EXCLUDE_PREFIXES = tuple(args.exclude_prefix)
    if args.exclude_contains: EXCLUDE_CONTAINS = tuple(args.exclude_contains)
    exclude_labels = set(args.exclude_labels or [])

    # ── Verify promtool is reachable ──────────────────────────────────────────
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
    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    out    = Path(args.out) if args.out else DATA_DIR / f"ml_export_{ts_str}.csv"

    print(f"Container  : {args.container}  ({args.data_dir})")
    print(f"Output     : {out}")
    print(f"Interval   : {args.interval}s  (WAL-mtime triggered)")
    print(f"Sandbox    : {'yes (--sandbox-dir-root)' if use_sandbox else 'no (promtool <3.x)'}")
    if METRIC_PREFIXES or METRIC_CONTAINS:
        print(f"Include    : prefix={METRIC_PREFIXES or '—'}  contains={METRIC_CONTAINS or '—'}")
    if EXCLUDE_PREFIXES or EXCLUDE_CONTAINS:
        print(f"Exclude    : prefix={EXCLUDE_PREFIXES or '—'}  contains={EXCLUDE_CONTAINS or '—'}")
    if exclude_labels:
        print(f"Drop labels: {sorted(exclude_labels)}")
    print("Press Ctrl-C to stop.\n")

    # ── Restore schema from existing CSV (for restart continuity) ─────────────
    fieldnames = read_schema(out)
    if fieldnames:
        print(f"Restored schema: {len(fieldnames)} columns from {out.name}\n")

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

            raw  = run_dump(args.container, args.data_dir, l_ms, use_sandbox)
            rows = parse_dump(raw, l_ms, exclude_labels)

            if not rows:
                print(f"  [{now_str}] #{poll}: no new rows")
            else:
                if fieldnames is None:
                    # First batch — discover schema from the data itself.
                    label_keys = discover_label_keys(rows)
                    fieldnames = BASE_FIELDS + label_keys
                    print(f"  [{now_str}] #{poll}: schema discovered — "
                          f"{len(label_keys)} label keys: {label_keys}")

                n = write_rows(rows, out, fieldnames)
                print(f"  [{now_str}] #{poll}: +{n} rows, "
                      f"{len(fieldnames)} cols → {out.name}")

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print(f"\nStopped after {poll} polls. Data in: {out}")


if __name__ == "__main__":
    main()
