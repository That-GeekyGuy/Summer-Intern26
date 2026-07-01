#!/usr/bin/env python3
"""
Tier 2 + TSFM dataset preparation pipeline.

Reads from either:
  - local CSV (default): prometheus_full_export_20260622_115327.csv at project root
  - MinIO Parquet store (legacy, pass --source=minio)

Steps:
  1. Load raw data (CSV or MinIO Parquet)
  2. Resample to 15s cadence (mean for gauges, last for counters)
  3. Convert counter columns to per-second rates (diff/dt, clip negatives → NaN for resets)
  4. Forward-fill slow (90s-cadence) columns up to 6 steps (90s)
  5. Compute binary label: uoi_binary = (uoi_value > q75 of training set)
  6. Construct MOMENT sliding windows (512 steps, 128-step stride)
  7. Construct Chronos-2 univariate series for uoi_value
  8. Write outputs to tools/train/data/

Outputs:
  tools/train/data/moment_windows.npz   — shape (n_windows, n_channels, 512)
  tools/train/data/moment_labels.npy    — shape (n_windows,) uoi_binary of last step
  tools/train/data/chronos_train.parquet — columns: timestamp, uoi_value
  tools/train/data/profile_report.json  — dataset statistics and warnings
  /models/dataset.parquet               — full feature matrix (legacy Tier 2 output)
  /models/feature_columns.json          — legacy column list

Usage:
  python train/prepare_dataset.py                      # local CSV source
  python train/prepare_dataset.py --source=minio       # MinIO Parquet source

Dataset: prometheus_full_export_20260622_115327.csv (~13 days of UPF data, 2026-06-22)
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────

REPO_ROOT         = Path(__file__).parent.parent.parent
DATA_DIR          = Path(__file__).parent / "data"
CSV_PATH          = REPO_ROOT / "prometheus_full_export_20260622_115327.csv"
SYNTHETIC_PARQUET = DATA_DIR / "synthetic_dataset.parquet"
MODELS_DIR        = Path(os.getenv("MODELS_DIR", str(REPO_ROOT / "models")))
OUTPUT_DIR        = DATA_DIR

# ── Constants ─────────────────────────────────────────────────────────────────

RESAMPLE_SECONDS  = 15          # target cadence after resampling
FFILL_SLOW_STEPS  = 6           # forward-fill slow (90s) channels up to 6 steps
MOMENT_SEQ_LEN    = 512         # MOMENT context window length
MOMENT_STRIDE     = 128         # stride for sliding window (75% overlap for train)
CHRONOS_HORIZON   = 20          # Chronos-2 forecast horizon steps
TRAIN_RATIO       = 0.80        # time-based train/eval split

# STL parameters (matching stl_service.py)
# Period = 96 steps × 15s = 24h (intraday). Weekly not possible with < 14 days of data.
STL_PERIOD        = int(os.getenv("STL_PERIOD", "96"))

# Traffic channels that have diurnal patterns — STL residuals replace raw values.
# Go runtime channels (goroutines, heap, gc) are aperiodic and excluded.
STL_TRAFFIC_CHANNELS = {
    "port_bytes_N3_rx_rate",
    "port_bytes_N6_tx_rate",
    "port_pkts_N3_rx_rate",
    "port_dropped_N3_rx_rate",
    "port_dropped_N6_rx_rate",
    "pfcp_sessions_total",
    "dl_throughput_efficiency_rate",
}

# Columns that are gauges at ~90s cadence — forward-fill up to FFILL_SLOW_STEPS
SLOW_CADENCE_COLS = [
    "tsi_value",
    "dl_forwarding_efficiency",
    "dl_throughput_efficiency_packet",
    "drop_rate_percentage",
]

# Counter columns: need diff/dt with reset handling
# These are matched by substring against actual column names
COUNTER_SUBSTRINGS = [
    "port_bytes_count",
    "port_packets_count",
    "port_dropped_count",
    "pfcp_messages_total",
    "pfcp_sessions_total",    # special: this is a cumulative counter in the CSV despite name
    "go_memstats_alloc_bytes_total",
    "go_memstats_frees_total",
    "go_memstats_mallocs_total",
    "go_gc_duration_seconds_count",
    "process_cpu_seconds_total",
    "process_network_receive_bytes_total",
    "process_network_transmit_bytes_total",
]

# Columns to drop entirely (zero-variance or metadata)
DROP_SUBSTRINGS = [
    "go_info",
    "go_gc_gogc_percent",
    "go_gc_gomemlimit_bytes",
    "process_start_time_seconds",
    "process_max_fds",
    "process_virtual_memory_max_bytes",
    "promhttp_",
    "scrape_",
    "up{",
    "_up_",
]

# MOMENT channel specification
# Maps logical name → substring to find in the wide DataFrame
# MOMENT_CHANNELS: logical name → one or more substrings that must ALL appear in the column name.
# Using multiple substrings avoids embedding device-specific instance IPs — any UPF deployment
# that exports these standard metrics will match regardless of the scrape target address.
MOMENT_CHANNELS = {
    "port_bytes_N3_rx_rate":         ("port_bytes_count{dir=rx_iface=N3",),
    "port_bytes_N6_tx_rate":         ("port_bytes_count{dir=tx_iface=N6",),
    "port_pkts_N3_rx_rate":          ("port_packets_count{dir=rx_iface=N3",),
    "port_dropped_N3_rx_rate":       ("port_dropped_count{dir=rx_iface=N3",),
    "port_dropped_N6_rx_rate":       ("port_dropped_count{dir=rx_iface=N6",),
    "pfcp_sessions_total":           ("pfcp_sessions_total{",),
    "pfcp_session_setup_rate":       ("pfcp_messages_total{", "direction=Incoming", "message_type=Session Establishment"),
    "dl_throughput_efficiency":      ("dl_throughput_efficiency_packet",),
    "dl_throughput_efficiency_rate": ("dl_throughput_efficiency_rate",),
    "drop_rate_percentage":          ("drop_rate_percentage",),
    "tsi_value":                     ("tsi_value",),
    "go_goroutines":                 ("go_goroutines{",),
    "go_heap_alloc_bytes":           ("go_memstats_heap_alloc_bytes{",),
    "gc_pressure_rate":              ("go_gc_duration_seconds_count{",),
}

# Channels that are already rates/gauges — do NOT apply diff() to these
NON_COUNTER_CHANNELS = {
    "pfcp_sessions_total",          # gauge in MOMENT context (absolute sessions, forward-filled)
    "dl_throughput_efficiency",
    "dl_throughput_efficiency_rate",
    "drop_rate_percentage",
    "tsi_value",
    "go_goroutines",
    "go_heap_alloc_bytes",
}


# ── CSV ingestion ─────────────────────────────────────────────────────────────

def _pivot_long_to_wide(df: pd.DataFrame) -> pd.DataFrame:
    """
    Convert long-format Prometheus export (metric_name + label columns + value)
    to wide format (one column per labeled series, one row per timestamp).

    Column name format: metric_name{key=val_key=val...} (sorted alphabetically, _ separator)
    This matches the substring patterns used in MOMENT_CHANNELS / COUNTER_SUBSTRINGS.
    """
    LABEL_COLS = [
        "code", "dir", "direction", "iface", "instance", "job", "le",
        "message_type", "node_id", "quantile", "reason", "result", "sliceid", "version",
    ]
    present = [c for c in LABEL_COLS if c in df.columns]

    # Convert Unix-seconds float timestamp to UTC datetime
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    df["value"] = pd.to_numeric(df["value"], errors="coerce")

    # Build series_key vectorised: accumulate "col=val" strings joined with _
    combined = pd.Series("", index=df.index, dtype=str)
    for col in present:
        s = df[col].astype(str)
        valid = df[col].notna() & (s != "nan") & (s.str.strip() != "")
        part = (col + "=" + s).where(valid, "")
        has_combined = combined.str.len() > 0
        has_part     = part.str.len() > 0
        mask_both    = has_combined & has_part
        mask_new     = (~has_combined) & has_part
        combined     = combined.where(~mask_both, combined + "_" + part)
        combined     = combined.where(~mask_new, part)

    df["_series_key"] = (
        df["metric_name"].astype(str)
        + np.where(combined.str.len() > 0, "{" + combined + "}", "")
    )

    n_series = df["_series_key"].nunique()
    log.info("pivoting %d rows × %d unique series → wide format …", len(df), n_series)
    wide = df[["timestamp", "_series_key", "value"]].pivot_table(
        index="timestamp", columns="_series_key", values="value", aggfunc="mean",
    )
    wide.columns.name = None
    wide = wide.reset_index()
    log.info("wide format: %d rows × %d columns", len(wide), len(wide.columns))
    return wide


def load_csv(path: Path) -> pd.DataFrame:
    """Load raw CSV in either wide or long Prometheus export format."""
    log.info("loading CSV from %s", path)
    df = pd.read_csv(str(path), low_memory=False, on_bad_lines="skip")

    if "metric_name" in df.columns:
        # Long-format (prometheus_full_export) — pivot to wide first
        log.info("detected long-format Prometheus export (%d rows, %d cols) — pivoting",
                 len(df), len(df.columns))
        df = _pivot_long_to_wide(df)
    else:
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.sort_values("timestamp").reset_index(drop=True)

    log.info("CSV loaded: %d rows, %d columns", len(df), len(df.columns))
    return df


def load_minio() -> pd.DataFrame:
    """Legacy MinIO path — loads Parquet files and pivots wide."""
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        log.error("boto3 not installed — run: pip install boto3")
        sys.exit(1)

    MINIO_ENDPOINT   = os.getenv("MINIO_ENDPOINT",   "minio:9000")
    MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY",  "")
    MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY",  "")
    MINIO_BUCKET     = os.getenv("MINIO_BUCKET",      "upf-metrics")

    s3 = boto3.client(
        "s3",
        endpoint_url=f"http://{MINIO_ENDPOINT}",
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY,
        config=Config(signature_version="s3v4"),
        region_name="us-east-1",
    )
    log.info("connecting to MinIO at %s bucket=%s", MINIO_ENDPOINT, MINIO_BUCKET)

    # List all Parquet keys and load them
    frames = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=MINIO_BUCKET):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".parquet"):
                data = s3.get_object(Bucket=MINIO_BUCKET, Key=obj["Key"])["Body"].read()
                frames.append(pd.read_parquet(io.BytesIO(data)))

    if not frames:
        log.error("no Parquet files found in MinIO — run the export job first")
        sys.exit(1)

    df = pd.concat(frames, ignore_index=True)
    if "timestamp_ms" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    log.info("MinIO data loaded: %d rows", len(df))
    return df


# ── Preprocessing ─────────────────────────────────────────────────────────────

def drop_metadata_cols(df: pd.DataFrame) -> pd.DataFrame:
    """Drop zero-variance, metadata, and scraper-internal columns."""
    to_drop = []
    for col in df.columns:
        if col == "timestamp":
            continue
        for sub in DROP_SUBSTRINGS:
            if sub in col:
                to_drop.append(col)
                break
    log.info("dropping %d metadata/zero-variance columns", len(to_drop))
    return df.drop(columns=to_drop, errors="ignore")


def _is_counter(col_name: str) -> bool:
    """True if this column should be rate-converted."""
    for sub in COUNTER_SUBSTRINGS:
        if sub in col_name:
            return True
    return False


def resample_and_rate(df: pd.DataFrame) -> pd.DataFrame:
    """
    Resample from raw ~2s cadence to 15s cadence.
    Counters: take last value, then apply diff/dt (clipped at 0 for resets).
    Gauges: take mean.
    """
    log.info("resampling to %ds cadence …", RESAMPLE_SECONDS)
    df = df.set_index("timestamp")

    rule = f"{RESAMPLE_SECONDS}s"
    counter_cols = [c for c in df.columns if _is_counter(c)]
    gauge_cols   = [c for c in df.columns if not _is_counter(c)]

    parts = []
    if gauge_cols:
        parts.append(df[gauge_cols].resample(rule).mean())
    if counter_cols:
        parts.append(df[counter_cols].resample(rule).last())

    wide = pd.concat(parts, axis=1) if len(parts) > 1 else parts[0]
    wide = wide.sort_index()
    log.info("after 15s resample: %d rows, %d columns", len(wide), len(wide.columns))

    # Rate conversion for counter columns
    dt = RESAMPLE_SECONDS  # seconds per step
    for col in counter_cols:
        if col not in wide.columns:
            continue
        d = wide[col].diff()
        d = d.clip(lower=0)   # counter reset → 0, not negative rate
        wide[col] = d / dt    # convert to per-second rate

    return wide.reset_index().rename(columns={"timestamp": "timestamp"})


def ffill_slow_channels(df: pd.DataFrame) -> pd.DataFrame:
    """Forward-fill slow 90s-cadence columns up to FFILL_SLOW_STEPS."""
    for logical_name in SLOW_CADENCE_COLS:
        # Match by substring in actual column names
        matched = [c for c in df.columns if logical_name in c]
        for col in matched:
            df[col] = df[col].ffill(limit=FFILL_SLOW_STEPS)
    return df


def compute_binary_label(df: pd.DataFrame, uoi_col: str, split_ts: pd.Timestamp) -> tuple[pd.Series, float]:
    """
    Compute binary anomaly label.
    Threshold = 75th percentile of uoi_value on TRAINING rows only (no leakage).
    Returns (binary_series, threshold_value).
    """
    train_mask = df["timestamp"] < split_ts
    train_uoi  = df.loc[train_mask, uoi_col].dropna()
    threshold  = float(train_uoi.quantile(0.75))
    log.info("uoi_value 75th percentile (train) = %.4f  →  binary label: uoi_value > %.4f", threshold, threshold)
    binary = (df[uoi_col] > threshold).astype(int)
    return binary, threshold


# ── Column finding helpers ────────────────────────────────────────────────────

def find_col(df: pd.DataFrame, *substrings: str) -> str | None:
    """Return the first column name containing ALL substrings, or None."""
    for col in df.columns:
        if all(s in col for s in substrings):
            return col
    return None


# ── STL residual extraction ───────────────────────────────────────────────────

def apply_stl_residuals(df: pd.DataFrame, channel_map: dict[str, str], period: int = STL_PERIOD) -> tuple[pd.DataFrame, dict]:
    """
    For each traffic channel, fit STL and replace the raw column with its residual component.
    Residuals capture "how much does this metric deviate from its seasonal norm?" which is
    more informative for MOMENT than raw values dominated by the seasonal trend.

    Go runtime channels (go_goroutines, go_heap_alloc_bytes, gc_pressure_rate) are aperiodic
    and kept as-is.

    Returns the modified df and a dict mapping logical_name → stl fit quality (r²).
    """
    try:
        from statsmodels.tsa.seasonal import STL
    except ImportError:
        log.warning("statsmodels not installed — skipping STL residuals, using raw values")
        return df, {}

    fit_quality = {}

    for logical_name, substrings in channel_map.items():
        if logical_name not in STL_TRAFFIC_CHANNELS:
            continue  # keep Go runtime channels raw

        col = find_col(df, *substrings)
        if col is None:
            continue

        series = df[col].copy().astype(float)

        # Impute NaN before fitting (STL requires complete series)
        if series.isna().any():
            series = series.ffill(limit=FFILL_SLOW_STEPS).bfill(limit=FFILL_SLOW_STEPS).fillna(0.0)

        n = len(series)
        if n < period * 2:
            log.warning("STL skip '%s': only %d rows, need >= %d", logical_name, n, period * 2)
            continue

        try:
            stl = STL(series.values, period=period, seasonal=7, robust=True)
            result = stl.fit()
            residual = result.resid

            # Residuals for MOMENT: mean-zero, captures anomalies as large positive spikes
            df[col] = residual

            # R² as fit quality proxy (variance explained by trend + seasonal)
            total_var = float(np.var(series.values))
            resid_var = float(np.var(residual))
            r2 = 1.0 - resid_var / (total_var + 1e-9)
            fit_quality[logical_name] = round(r2, 4)
            log.info("STL residuals '%s': r²=%.3f  resid_std=%.4g", logical_name, r2, float(residual.std()))

        except Exception as e:
            log.warning("STL fit failed for '%s': %s — keeping raw values", logical_name, e)

    return df, fit_quality


# ── MOMENT window construction ────────────────────────────────────────────────

def build_moment_windows(
    df: pd.DataFrame,
    channel_map: dict[str, tuple[str, ...]],
    binary_label_col: str,
    seq_len: int = MOMENT_SEQ_LEN,
    stride: int  = MOMENT_STRIDE,
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """
    Build sliding windows for MOMENT reconstruction.

    Returns:
        windows: ndarray (n_windows, n_channels, seq_len)
        labels:  ndarray (n_windows,) — uoi_binary of LAST step in window
        channel_names: list of logical channel names (ordered)
    """
    # Build channel matrix
    channel_names = []
    channel_arrays = []

    for logical_name, substrings in channel_map.items():
        col = find_col(df, *substrings)
        if col is None:
            log.warning("MOMENT channel '%s' (substrings %s) NOT FOUND — filling with zeros",
                        logical_name, substrings)
            arr = np.zeros(len(df), dtype=np.float32)
        else:
            arr = df[col].astype(float).values.astype(np.float32)

        # Impute NaN with column mean
        nan_mask = np.isnan(arr)
        if nan_mask.any():
            col_mean = float(np.nanmean(arr)) if not np.all(nan_mask) else 0.0
            arr[nan_mask] = col_mean

        # Standardize (z-score) per channel — MOMENT works better with normalized inputs
        std = arr.std()
        if std > 1e-9:
            arr = (arr - arr.mean()) / std

        channel_names.append(logical_name)
        channel_arrays.append(arr)

    matrix = np.stack(channel_arrays, axis=0)  # (n_channels, n_rows)
    labels_full = df[binary_label_col].values.astype(np.int32)

    n_rows = matrix.shape[1]
    windows = []
    labels  = []

    i = 0
    while i + seq_len <= n_rows:
        win = matrix[:, i:i + seq_len]           # (n_channels, seq_len)
        lbl = int(labels_full[i + seq_len - 1])  # label of LAST step
        windows.append(win)
        labels.append(lbl)
        i += stride

    if len(windows) == 0:
        log.error("no MOMENT windows created — dataset too short (%d rows, need >= %d)",
                  n_rows, seq_len)
        return np.empty((0, len(channel_names), seq_len)), np.empty(0), channel_names

    log.info("MOMENT windows: %d  (rows=%d, seq_len=%d, stride=%d, channels=%d)",
             len(windows), n_rows, seq_len, stride, len(channel_names))
    return np.stack(windows, axis=0), np.array(labels), channel_names


# ── Chronos-2 series construction ────────────────────────────────────────────

def build_chronos_series(df: pd.DataFrame, uoi_col: str) -> pd.DataFrame:
    """Build the univariate uoi_value series for Chronos-2 training."""
    series = df[["timestamp", uoi_col]].copy()
    series = series.rename(columns={uoi_col: "uoi_value"})
    # Drop rows where uoi_value is still NaN after ffill
    series = series.dropna(subset=["uoi_value"]).reset_index(drop=True)
    log.info("Chronos series: %d rows (after dropping NaN uoi_value)", len(series))
    return series


# ── Legacy dataset output (for existing Tier 2 server.py) ────────────────────

def _stratified_episode_split(
    df: pd.DataFrame, ratio: float, out_dir: Path
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """
    Scenario-aware stratified split.

    Each contiguous block of anomaly rows is an "episode". The trailing
    (1-ratio) fraction of every episode goes to eval, the rest to train.
    Normal rows use a standard time-ordered split. This guarantees every
    episode appears in both partitions, fixing 0% anomaly recall caused by
    a pure time split when all anomaly windows fall in the first 80%.
    """
    df = df.sort_values("timestamp").reset_index(drop=True)

    train_parts: list[pd.DataFrame] = []
    eval_parts:  list[pd.DataFrame] = []

    normal_df = df[df["scenario_id"] == 0]
    split_idx = int(len(normal_df) * ratio)
    train_parts.append(normal_df.iloc[:split_idx])
    eval_parts.append(normal_df.iloc[split_idx:])

    episode_stats: list[dict] = []
    for ep_id in sorted(df[df["scenario_id"] > 0]["scenario_id"].unique()):
        ep = df[df["scenario_id"] == ep_id]
        n       = len(ep)
        n_eval  = max(1, int(n * (1 - ratio)))
        n_train = n - n_eval
        train_parts.append(ep.iloc[:n_train])
        eval_parts.append(ep.iloc[n_train:])
        episode_stats.append({
            "episode_id": int(ep_id),
            "total_rows": n,
            "n_train":    n_train,
            "n_eval":     n_eval,
            "ts_start":   str(ep["timestamp"].iloc[0]),
            "ts_end":     str(ep["timestamp"].iloc[-1]),
        })

    df_train = pd.concat(train_parts).sort_values("timestamp").reset_index(drop=True)
    df_eval  = pd.concat(eval_parts).sort_values("timestamp").reset_index(drop=True)

    pq.write_table(pa.Table.from_pandas(df_train), str(out_dir / "dataset_train.parquet"))
    pq.write_table(pa.Table.from_pandas(df_eval),  str(out_dir / "dataset_eval.parquet"))

    anom_train = int((df_train["label"] != "normal").sum())
    anom_eval  = int((df_eval["label"]  != "normal").sum())
    total_anom = anom_train + anom_eval

    report = {
        "generated_at":          datetime.now(timezone.utc).isoformat(),
        "split_ratio":           ratio,
        "n_episodes":            len(episode_stats),
        "n_train":               len(df_train),
        "n_eval":                len(df_eval),
        "anomaly_rows_train":    anom_train,
        "anomaly_rows_eval":     anom_eval,
        "anomaly_eval_fraction": round(anom_eval / total_anom, 4) if total_anom > 0 else 0.0,
        "episodes":              episode_stats,
    }
    (out_dir / "split_report.json").write_text(json.dumps(report, indent=2))
    log.info(
        "stratified split → train=%d  eval=%d  anomaly_in_eval=%d  episodes=%d",
        len(df_train), len(df_eval), anom_eval, len(episode_stats),
    )
    return df_train, df_eval, report


def build_legacy_dataset(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """
    Reproduce the feature engineering from the original prepare_dataset.py
    so the existing ml-infer sidecar continues to work unchanged.
    """
    EPSILON   = 1e-9
    ROLLING_W = 20  # 5 min at 15s

    # Find columns by substring
    sess_col     = find_col(df, "pfcp_sessions_total{")
    bytes_n3_rx  = find_col(df, "port_bytes_count{dir=rx_iface=N3")
    bytes_n3_tx  = find_col(df, "port_bytes_count{dir=tx_iface=N3")
    bytes_n6_rx  = find_col(df, "port_bytes_count{dir=rx_iface=N6")
    bytes_n6_tx  = find_col(df, "port_bytes_count{dir=tx_iface=N6")
    drops_n3     = find_col(df, "port_dropped_count{dir=rx_iface=N3")
    drops_n6     = find_col(df, "port_dropped_count{dir=rx_iface=N6")
    pkts_n3_rx   = find_col(df, "port_packets_count{dir=rx_iface=N3")
    pkts_n6_rx   = find_col(df, "port_packets_count{dir=rx_iface=N6")

    w = pd.DataFrame({"timestamp": df["timestamp"]})

    def _get(col):
        if col and col in df.columns:
            return df[col].values.astype(float)
        return np.zeros(len(df), dtype=float)

    # Rates already computed in resample step — columns are now /second
    w["rate_pfcp_sessions"] = _get(sess_col)
    w["rate_bytes_N3_rx"]   = _get(bytes_n3_rx)
    w["rate_bytes_N3_tx"]   = _get(bytes_n3_tx)
    w["rate_bytes_N6_rx"]   = _get(bytes_n6_rx)
    w["rate_bytes_N6_tx"]   = _get(bytes_n6_tx)
    w["rate_drops_N3"]      = _get(drops_n3)
    w["rate_drops_N6"]      = _get(drops_n6)
    _pkts_n3 = _get(pkts_n3_rx)
    _pkts_n6 = _get(pkts_n6_rx)

    # Ratio features
    w["rx_tx_ratio_N3"]   = w["rate_bytes_N3_rx"] / (w["rate_bytes_N3_tx"] + EPSILON)
    w["rx_tx_ratio_N6"]   = w["rate_bytes_N6_rx"] / (w["rate_bytes_N6_tx"] + EPSILON)
    w["drop_fraction_N3"] = w["rate_drops_N3"] / (_pkts_n3 + EPSILON)
    w["drop_fraction_N6"] = w["rate_drops_N6"] / (_pkts_n6 + EPSILON)

    RATE_FEATURES = [
        "rate_pfcp_sessions",
        "rate_bytes_N3_rx", "rate_bytes_N3_tx",
        "rate_bytes_N6_rx", "rate_bytes_N6_tx",
        "rate_drops_N3", "rate_drops_N6",
    ]
    RATIO_FEATURES = ["rx_tx_ratio_N3", "rx_tx_ratio_N6", "drop_fraction_N3", "drop_fraction_N6"]

    # Rolling statistics
    for feat in RATE_FEATURES:
        roll = w[feat].rolling(window=ROLLING_W, min_periods=1)
        w[f"{feat}_mean_5m"] = roll.mean()
        w[f"{feat}_std_5m"]  = roll.std().fillna(0)
        w[f"{feat}_min_5m"]  = roll.min()
        w[f"{feat}_max_5m"]  = roll.max()

    ROLLING_FEATURES = [f"{f}_{s}_5m" for f in RATE_FEATURES for s in ("mean", "std", "min", "max")]
    FEATURE_COLS = RATE_FEATURES + RATIO_FEATURES + ROLLING_FEATURES

    # Label: normal / anomaly
    tsi_col = find_col(df, "tsi_value")
    if tsi_col:
        split_ts = df["timestamp"].quantile(TRAIN_RATIO)
        _, threshold = compute_binary_label(df, tsi_col, split_ts)
        label_raw = (df[tsi_col].ffill(limit=FFILL_SLOW_STEPS) > threshold).astype(int)
        w["label"] = label_raw.map({0: "normal", 1: "anomaly"}).fillna("normal")
    else:
        w["label"] = "normal"

    # Episode IDs: each contiguous block of anomaly rows gets a unique integer ID.
    # scenario_id=0 means normal. Used by _stratified_episode_split to ensure every
    # episode appears in both train and eval, fixing 0% recall from pure time splits.
    is_anom = w["label"] != "normal"
    ep_start = is_anom & (~is_anom.shift(1, fill_value=False))
    w["scenario_id"] = np.where(is_anom, ep_start.cumsum(), 0).astype(int)

    return w[FEATURE_COLS + ["label", "scenario_id", "timestamp"]], FEATURE_COLS


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run(source: str = "local"):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    # ── 1. Load ───────────────────────────────────────────────────────────────
    synthetic_source = source == "synthetic"

    if source == "minio":
        df = load_minio()
    elif synthetic_source:
        if not SYNTHETIC_PARQUET.exists():
            log.error(
                "synthetic_dataset.parquet not found at %s — run generate_synthetic_data.py first",
                SYNTHETIC_PARQUET,
            )
            sys.exit(1)
        df = pd.read_parquet(str(SYNTHETIC_PARQUET))
        if "timestamp" not in df.columns:
            log.error("synthetic parquet missing 'timestamp' column")
            sys.exit(1)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        log.info("synthetic dataset loaded: %d rows, %d columns", len(df), len(df.columns))
    else:
        if not CSV_PATH.exists():
            log.error("CSV not found at %s — pass --source=minio or --source=synthetic", CSV_PATH)
            sys.exit(1)
        df = load_csv(CSV_PATH)

    if not synthetic_source:
        # ── 2. Drop metadata columns ──────────────────────────────────────────
        df = drop_metadata_cols(df)

        # ── 3. Resample + rate conversion ─────────────────────────────────────
        df = resample_and_rate(df)
        log.info("after resample: %d rows", len(df))

        # ── 4. Forward-fill slow channels ─────────────────────────────────────
        df = ffill_slow_channels(df)
    else:
        # Synthetic data is already at 15s cadence with rates pre-computed; no NaN
        log.info("synthetic source: skipping resample/rate-conversion/ffill steps")

    # ── 5. Time-based split point (for label calibration) ────────────────────
    n = len(df)
    split_idx = int(n * TRAIN_RATIO)
    split_ts  = df["timestamp"].iloc[split_idx]
    log.info("train/eval split at row %d  (%s)", split_idx, split_ts)

    # ── 6. Binary label ───────────────────────────────────────────────────────
    uoi_col = find_col(df, "tsi_value")
    if uoi_col is None:
        log.error("tsi_value column not found — check CSV column names")
        sys.exit(1)

    df["uoi_binary"], uoi_threshold = compute_binary_label(df, uoi_col, split_ts)

    total_labeled = df["uoi_binary"].notna().sum()
    n_anomaly     = int(df["uoi_binary"].sum())
    n_normal      = int(total_labeled - n_anomaly)
    anomaly_frac  = n_anomaly / total_labeled if total_labeled > 0 else 0.0
    log.info("binary labels: normal=%d  anomaly=%d  (%.1f%% anomalous)", n_normal, n_anomaly, anomaly_frac * 100)

    # ── 7. STL residuals for traffic channels ────────────────────────────────
    # Auto-select STL period: weekly (672 steps = 7 days × 96 steps/day) when
    # dataset spans ≥ 14 days; daily (96 steps) otherwise.
    span_days = (df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]).days
    stl_period_effective = 672 if span_days >= 14 else STL_PERIOD
    if stl_period_effective != STL_PERIOD:
        log.info(
            "STL period auto-upgraded: %d steps (weekly) for %d-day dataset",
            stl_period_effective, span_days,
        )

    df, stl_quality = apply_stl_residuals(df, MOMENT_CHANNELS, period=stl_period_effective)
    if stl_quality:
        log.info("STL residuals applied to %d channels", len(stl_quality))
    else:
        log.info("STL residuals skipped — using raw values for all MOMENT channels")

    # ── 8. MOMENT windows ─────────────────────────────────────────────────────
    # Fill remaining NaN in uoi_binary with 0 (treat as normal if uoi unknown)
    df["uoi_binary"] = df["uoi_binary"].fillna(0).astype(int)

    windows, labels, channel_names = build_moment_windows(
        df, MOMENT_CHANNELS, "uoi_binary",
        seq_len=MOMENT_SEQ_LEN, stride=MOMENT_STRIDE,
    )

    n_windows = len(windows)
    n_train_windows = int(n_windows * TRAIN_RATIO)
    n_eval_windows  = n_windows - n_train_windows
    n_anomaly_windows = int(labels.sum()) if n_windows > 0 else 0

    warnings = []

    if n_windows < 10:
        msg = (f"DATA SUFFICIENCY WARNING: only {n_windows} MOMENT windows generated "
               f"(need ≥ 10 for meaningful training). Consider longer data collection.")
        warnings.append(msg)
        log.warning("⚠ %s", msg)

    # Count distinct overload episodes in eval set
    if n_windows > 0:
        eval_labels = labels[n_train_windows:]
        # Episode = contiguous run of 1s
        episodes = 0
        in_ep = False
        for lbl in eval_labels:
            if lbl == 1 and not in_ep:
                episodes += 1
                in_ep = True
            elif lbl == 0:
                in_ep = False
        if episodes < 3:
            msg = (f"DATA SUFFICIENCY WARNING: only {episodes} distinct overload episodes "
                   f"in eval set (< 3). MOMENT evaluation metrics may not be reliable.")
            warnings.append(msg)
            log.warning("⚠ %s", msg)

    # Save MOMENT windows
    np.savez_compressed(
        str(OUTPUT_DIR / "moment_windows.npz"),
        windows=windows,
        labels=labels,
    )
    np.save(str(OUTPUT_DIR / "moment_labels.npy"), labels)
    log.info("saved moment_windows.npz  shape=%s", windows.shape if n_windows > 0 else "(0,)")
    log.info("saved moment_labels.npy   shape=%s  anomaly_fraction=%.1f%%",
             labels.shape, float(labels.mean() * 100) if n_windows > 0 else 0.0)

    # ── 9. Chronos-2 series ───────────────────────────────────────────────────
    chronos_df = build_chronos_series(df, uoi_col)
    chronos_path = OUTPUT_DIR / "chronos_train.parquet"
    chronos_df.to_parquet(str(chronos_path), index=False)
    log.info("saved chronos_train.parquet  rows=%d", len(chronos_df))

    # ── 10. Legacy dataset output ─────────────────────────────────────────────
    legacy_df, feature_cols = build_legacy_dataset(df)
    legacy_df = legacy_df.dropna(subset=["rate_pfcp_sessions"])

    out_path = MODELS_DIR / "dataset.parquet"
    table = pa.Table.from_pandas(legacy_df)
    pq.write_table(table, str(out_path))
    log.info("saved legacy dataset.parquet  rows=%d", len(legacy_df))

    feat_path = MODELS_DIR / "feature_columns.json"
    feat_path.write_text(json.dumps(feature_cols, indent=2))

    # ── 10b. Scenario-aware stratified split ──────────────────────────────────
    # Splits legacy_df into dataset_train.parquet + dataset_eval.parquet such that
    # every anomaly episode appears in both partitions. Pure time-split puts all
    # episodes in train (0% eval recall) when episodes are short vs. dataset length.
    _, _, split_report = _stratified_episode_split(legacy_df, TRAIN_RATIO, MODELS_DIR)

    # ── 11. Channel names for MOMENT ─────────────────────────────────────────
    (MODELS_DIR / "moment_channel_names.json").write_text(json.dumps(channel_names, indent=2))
    log.info("saved moment_channel_names.json  channels=%d", len(channel_names))

    # ── 12. Profile report ───────────────────────────────────────────────────
    profile = {
        "generated_at":      datetime.now(timezone.utc).isoformat(),
        "source":            source,
        "raw_rows":          n,
        "resampled_rows":    len(df),
        "resample_cadence_s": RESAMPLE_SECONDS,
        "uoi_threshold_q75": uoi_threshold,
        "uoi_anomaly_fraction": round(anomaly_frac, 4),
        "n_normal_rows":     n_normal,
        "n_anomaly_rows":    n_anomaly,
        "moment": {
            "n_windows":         n_windows,
            "n_train_windows":   n_train_windows,
            "n_eval_windows":    n_eval_windows,
            "n_anomaly_windows": n_anomaly_windows,
            "seq_len":           MOMENT_SEQ_LEN,
            "stride":            MOMENT_STRIDE,
            "n_channels":        len(channel_names),
            "channel_names":     channel_names,
        },
        "chronos": {
            "n_rows":       len(chronos_df),
            "context_len":  MOMENT_SEQ_LEN,
            "horizon":      CHRONOS_HORIZON,
        },
        "stl_residuals": {
            "period_steps":    stl_period_effective,
            "period_seconds":  stl_period_effective * RESAMPLE_SECONDS,
            "channels_fitted": list(stl_quality.keys()),
            "fit_quality_r2":  stl_quality,
            "note": "Traffic channels use STL residuals; Go runtime channels use raw values.",
        },
        "warnings": warnings,
        "stratified_split": split_report,
    }
    profile_path = OUTPUT_DIR / "profile_report.json"
    profile_path.write_text(json.dumps(profile, indent=2))
    log.info("saved profile_report.json")

    log.info("data preparation complete — %d warnings", len(warnings))
    for w in warnings:
        log.warning("  ⚠ %s", w)

    return profile


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prepare TSFM + legacy ML dataset")
    parser.add_argument(
        "--source",
        choices=["local", "minio", "synthetic"],
        default="local",
        help=(
            "Data source: "
            "'local' reads prometheus_full_export_20260622_115327.csv, "
            "'minio' reads from MinIO Parquet store, "
            "'synthetic' reads tools/train/data/synthetic_dataset.parquet "
            "(generated by generate_synthetic_data.py)"
        ),
    )
    args = parser.parse_args()
    run(source=args.source)
