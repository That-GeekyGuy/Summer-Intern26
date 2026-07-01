#!/usr/bin/env python3
"""
Synthetic 1-year UPF metrics dataset generator.

Generates realistic 5G User Plane Function (UPF) metrics for training ML anomaly
detection and forecasting models. Output is wide-format Parquet at 15s cadence.

Patterns modeled
────────────────
  Diurnal     : business hours (08:00-18:00 UTC) 1.5× base, night 0.3× base
  Weekly      : Mon-Fri full, Sat 0.6×, Sun 0.4×
  Holidays    : Indian public holidays → 0.3× weekday baseline
  Maintenance : 2-3 windows/month, 2-4h, near-zero traffic + elevated drops
  Anomaly     : ~4% of timesteps in clustered bursts (5 scenario types)

Anomaly scenarios
─────────────────
  1  session_overload  — pfcp_sessions spike 3×, tsi spike, efficiency drop
  2  traffic_spike     — port_bytes 5×, drops spike, tsi spike
  3  datapath_fault    — port_dropped 10×, efficiency near 0, tsi spike
  4  memory_pressure   — go_heap 3×, goroutines 2×, gc_rate 5×
  5  maintenance       — all traffic near 0, drops moderate, tsi near 0

Usage
─────
  cd tools
  python train/generate_synthetic_data.py
  python train/generate_synthetic_data.py --start 2026-01-01 --days 365
  python train/generate_synthetic_data.py --start 2026-01-01 --days 30   # quick test

Output
──────
  tools/train/data/synthetic_dataset.parquet   (~80MB, ~2.1M rows for 365 days)
  tools/train/data/synthetic_profile.json      (stats summary)

Then run:
  python train/prepare_dataset.py --source=synthetic
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

DATA_DIR = Path(__file__).parent / "data"

# ── Indian public holidays (month-day) ──────────────────────────────────────
# Covers 2026 but the format works for any year generated.
INDIA_HOLIDAYS_MD = {
    "01-26",  # Republic Day
    "03-14",  # Holi (approx 2026)
    "04-01",  # Eid-ul-Fitr (approx 2026)
    "04-07",  # Ram Navami
    "04-14",  # Dr. Ambedkar Jayanti
    "05-01",  # Labour Day / Maharashtra Day
    "06-07",  # Eid-ul-Adha (approx 2026)
    "08-15",  # Independence Day
    "08-16",  # Janmashtami (approx 2026)
    "08-27",  # Ganesh Chaturthi (approx 2026)
    "10-02",  # Gandhi Jayanti
    "10-20",  # Diwali (approx 2026)
    "10-21",  # Diwali second day
    "11-15",  # Guru Nanak Jayanti
    "12-25",  # Christmas
}

# ── Baseline metric stats (mean, std, min_clip, max_clip) ───────────────────
# Column names use substrings matching prepare_dataset.py MOMENT_CHANNELS +
# COUNTER_SUBSTRINGS patterns so find_col() resolves them correctly.
BASELINES: dict[str, tuple[float, float, float | None, float | None]] = {
    # Bytes/sec (already rates — synthetic path skips counter diff step)
    "port_bytes_count{dir=rx_iface=N3_synthetic}": (5e6,   2e6,   0.0, None),
    "port_bytes_count{dir=tx_iface=N6_synthetic}": (4.5e6, 1.8e6, 0.0, None),
    "port_bytes_count{dir=rx_iface=N6_synthetic}": (200e3, 80e3,  0.0, None),
    "port_bytes_count{dir=tx_iface=N3_synthetic}": (250e3, 90e3,  0.0, None),
    # Packets/sec
    "port_packets_count{dir=rx_iface=N3_synthetic}": (8000, 3000, 0.0, None),
    "port_packets_count{dir=rx_iface=N6_synthetic}": (300,  100,  0.0, None),
    # Drops/sec
    "port_dropped_count{dir=rx_iface=N3_synthetic}": (5,    8,    0.0, None),
    "port_dropped_count{dir=rx_iface=N6_synthetic}": (2,    3,    0.0, None),
    # PFCP gauges / rates
    "pfcp_sessions_total{synthetic}":                (2000, 400,  0.0, None),
    "pfcp_messages_total{direction=Incoming_message_type=Session Establishment_synthetic}": (50, 20, 0.0, None),
    # DL efficiency (gauges 0-1)
    "dl_throughput_efficiency_packet{synthetic}":    (0.92, 0.03, 0.0, 1.0),
    "dl_throughput_efficiency_rate{synthetic}":      (0.91, 0.04, 0.0, 1.0),
    # Drop rate percentage (gauge 0-100)
    "drop_rate_percentage{synthetic}":               (0.15, 0.30, 0.0, 100.0),
    # TSI value — the primary UOI / label signal (gauge 0-1)
    "tsi_value{synthetic}":                          (0.35, 0.15, 0.0, 1.0),
    # Go runtime (aperiodic — NOT traffic-modulated)
    "go_goroutines{synthetic}":                      (25,   5,    1.0, None),
    "go_memstats_heap_alloc_bytes{synthetic}":       (180e6, 40e6, 10e6, None),
    "go_gc_duration_seconds_count{synthetic}":       (0.03, 0.01, 0.0, None),
}

# Go runtime metrics are aperiodic — skip diurnal/weekly scaling
APERIODIC_COLS = {
    "go_goroutines{synthetic}",
    "go_memstats_heap_alloc_bytes{synthetic}",
    "go_gc_duration_seconds_count{synthetic}",
}


def _diurnal_scale(hour_frac: float) -> float:
    """Traffic multiplier [0.3, 1.5] for fractional hour-of-day (UTC)."""
    if 8.0 <= hour_frac < 18.0:
        phase = (hour_frac - 8.0) / 10.0 * np.pi
        return float(0.9 + 0.6 * np.sin(phase))
    elif 18.0 <= hour_frac < 23.0:
        return float(0.8 - 0.4 * (hour_frac - 18.0) / 5.0)
    return 0.3


def _weekly_scale(weekday: int) -> float:
    """Traffic multiplier for weekday (0=Mon … 6=Sun)."""
    return [1.0, 1.0, 1.0, 1.0, 1.0, 0.6, 0.4][weekday]


def _build_scale_vector(timestamps: pd.DatetimeIndex) -> np.ndarray:
    """Vectorised per-timestamp traffic scale (accounts for diurnal + weekly + holidays)."""
    scales = np.empty(len(timestamps), dtype=np.float32)
    for i, ts in enumerate(timestamps):
        if ts.strftime("%m-%d") in INDIA_HOLIDAYS_MD:
            scales[i] = 0.3
        else:
            h = ts.hour + ts.minute / 60.0
            scales[i] = _diurnal_scale(h) * _weekly_scale(ts.weekday())
    return scales


def _plan_anomaly_windows(
    n: int, rng: np.random.Generator, target_frac: float = 0.04
) -> list[tuple[int, int, int]]:
    """
    Plan anomaly burst windows as (start_idx, end_idx, scenario_id).
    scenario_id: 1=session_overload 2=traffic_spike 3=datapath_fault
                 4=memory_pressure 5=maintenance
    Anomalies are clustered (not uniformly distributed) to mimic real incident patterns.
    """
    windows: list[tuple[int, int, int]] = []
    target = int(n * target_frac)
    covered = 0
    i = 100  # skip first ~25 min to avoid cold-start artifacts

    while i < n - 200 and covered < target:
        gap = int(rng.integers(120, 1200))   # 30 min – 5h gap between clusters
        i += gap
        if i >= n - 100:
            break

        cluster = int(rng.integers(1, 4))  # 1-3 incidents per cluster
        for _ in range(cluster):
            if i >= n - 50:
                break
            duration = int(rng.integers(20, 240))  # 5 min – 60 min per incident
            scenario = int(rng.integers(1, 6))
            end = min(i + duration, n - 1)
            windows.append((i, end, scenario))
            covered += end - i
            i = end + int(rng.integers(15, 60))  # brief gap within cluster

    log.info(
        "planned %d anomaly windows ≈ %.1f%% of timesteps",
        len(windows), covered / n * 100,
    )
    return windows


def _hanning_envelope(length: int) -> np.ndarray:
    if length <= 2:
        return np.ones(length, dtype=np.float32)
    return np.hanning(length).astype(np.float32)


def _apply_anomaly(
    df: pd.DataFrame,
    start: int,
    end: int,
    scenario: int,
) -> None:
    """In-place anomaly injection with smooth Hanning ramp-in/ramp-out."""
    length = end - start + 1
    env = _hanning_envelope(length)
    slice(start, end + 1)

    if scenario == 1:  # session_overload
        df.loc[start:end, "pfcp_sessions_total{synthetic}"] *= (1 + 2.0 * env)
        df.loc[start:end, "pfcp_messages_total{direction=Incoming_message_type=Session Establishment_synthetic}"] *= (1 + 3.0 * env)
        df.loc[start:end, "dl_throughput_efficiency_packet{synthetic}"] = np.clip(
            df.loc[start:end, "dl_throughput_efficiency_packet{synthetic}"].values - 0.2 * env, 0, 1)
        df.loc[start:end, "drop_rate_percentage{synthetic}"] = np.clip(
            df.loc[start:end, "drop_rate_percentage{synthetic}"].values + 15 * env, 0, 100)
        df.loc[start:end, "tsi_value{synthetic}"] = np.clip(
            df.loc[start:end, "tsi_value{synthetic}"].values + 0.50 * env, 0, 1)

    elif scenario == 2:  # traffic_spike
        for col in [
            "port_bytes_count{dir=rx_iface=N3_synthetic}",
            "port_bytes_count{dir=tx_iface=N6_synthetic}",
            "port_packets_count{dir=rx_iface=N3_synthetic}",
        ]:
            df.loc[start:end, col] *= (1 + 4.0 * env)
        df.loc[start:end, "port_dropped_count{dir=rx_iface=N3_synthetic}"] *= (1 + 10 * env)
        df.loc[start:end, "drop_rate_percentage{synthetic}"] = np.clip(
            df.loc[start:end, "drop_rate_percentage{synthetic}"].values + 8 * env, 0, 100)
        df.loc[start:end, "tsi_value{synthetic}"] = np.clip(
            df.loc[start:end, "tsi_value{synthetic}"].values + 0.45 * env, 0, 1)

    elif scenario == 3:  # datapath_fault
        df.loc[start:end, "port_dropped_count{dir=rx_iface=N3_synthetic}"] *= (1 + 20 * env)
        df.loc[start:end, "port_dropped_count{dir=rx_iface=N6_synthetic}"] *= (1 + 15 * env)
        df.loc[start:end, "dl_throughput_efficiency_packet{synthetic}"] = np.clip(
            df.loc[start:end, "dl_throughput_efficiency_packet{synthetic}"].values * (1 - 0.7 * env), 0, 1)
        df.loc[start:end, "dl_throughput_efficiency_rate{synthetic}"] = np.clip(
            df.loc[start:end, "dl_throughput_efficiency_rate{synthetic}"].values * (1 - 0.7 * env), 0, 1)
        df.loc[start:end, "drop_rate_percentage{synthetic}"] = np.clip(
            df.loc[start:end, "drop_rate_percentage{synthetic}"].values + 30 * env, 0, 100)
        df.loc[start:end, "tsi_value{synthetic}"] = np.clip(
            df.loc[start:end, "tsi_value{synthetic}"].values + 0.55 * env, 0, 1)

    elif scenario == 4:  # memory_pressure
        df.loc[start:end, "go_memstats_heap_alloc_bytes{synthetic}"] *= (1 + 2.5 * env)
        df.loc[start:end, "go_goroutines{synthetic}"] *= (1 + 1.5 * env)
        df.loc[start:end, "go_gc_duration_seconds_count{synthetic}"] *= (1 + 5.0 * env)
        df.loc[start:end, "tsi_value{synthetic}"] = np.clip(
            df.loc[start:end, "tsi_value{synthetic}"].values + 0.30 * env, 0, 1)

    elif scenario == 5:  # maintenance — all traffic near zero
        traffic_cols = [c for c in BASELINES if c not in APERIODIC_COLS and "tsi" not in c]
        for col in traffic_cols:
            df.loc[start:end, col] *= np.maximum(0.05, 1 - 0.9 * env)
        df.loc[start:end, "port_dropped_count{dir=rx_iface=N3_synthetic}"] *= (1 + 5 * env)
        # TSI drops during maintenance (near-zero traffic = lower UOI)
        df.loc[start:end, "tsi_value{synthetic}"] = np.clip(
            df.loc[start:end, "tsi_value{synthetic}"].values * (1 - 0.7 * env), 0, 1)


def generate(start_date: str = "2026-01-01", days: int = 365, seed: int = 42) -> pd.DataFrame:
    """Generate and return the synthetic wide-format DataFrame."""
    rng = np.random.default_rng(seed)

    start = pd.Timestamp(start_date, tz="UTC")
    n = days * 24 * 60 * 4  # steps at 15s cadence
    timestamps = pd.date_range(start=start, periods=n, freq="15s", tz="UTC")
    log.info("generating %d rows (%d days × 4 steps/min) …", n, days)

    # Build per-timestamp traffic scale (diurnal + weekly + holidays)
    log.info("computing scale vector …")
    scale = _build_scale_vector(timestamps)

    # Generate base time series per column with AR(1) noise
    log.info("generating base signals with AR(1) temporal correlation …")
    data: dict[str, object] = {"timestamp": timestamps}
    for col, (mean, std, lo, hi) in BASELINES.items():
        # AR(1) with ρ=0.85 for realistic temporal autocorrelation
        noise = rng.standard_normal(n).astype(np.float32)
        rho = 0.85
        phi = float(np.sqrt(1 - rho**2))
        for i in range(1, n):
            noise[i] = rho * noise[i - 1] + phi * noise[i]

        vals = mean + std * noise
        if col not in APERIODIC_COLS:
            vals *= scale
        vals = vals.astype(np.float32)
        if lo is not None:
            vals = np.clip(vals, lo, None)
        if hi is not None:
            vals = np.clip(vals, None, hi)
        data[col] = vals

    df = pd.DataFrame(data)

    # Inject anomaly bursts
    log.info("planning and injecting anomaly windows …")
    windows = _plan_anomaly_windows(n, rng)
    df["scenario_id"] = np.int32(0)
    for s, e, sc in windows:
        _apply_anomaly(df, s, e, sc)
        df.loc[s:e, "scenario_id"] = np.int32(sc)

    # Final clip after anomaly injection
    for col, (_, _, lo, hi) in BASELINES.items():
        if lo is not None:
            df[col] = df[col].clip(lower=lo)
        if hi is not None:
            df[col] = df[col].clip(upper=hi)

    anom_frac = (df["scenario_id"] > 0).mean()
    log.info("anomaly fraction: %.2f%%", anom_frac * 100)
    return df


def save(df: pd.DataFrame) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = DATA_DIR / "synthetic_dataset.parquet"
    df.to_parquet(str(out), index=False, compression="snappy")
    size_mb = out.stat().st_size / 1e6
    log.info("saved %s  rows=%d  %.1fMB", out.name, len(df), size_mb)

    sc_names = {1: "session_overload", 2: "traffic_spike", 3: "datapath_fault",
                4: "memory_pressure", 5: "maintenance"}
    profile = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_rows": int(len(df)),
        "start": str(df["timestamp"].iloc[0]),
        "end": str(df["timestamp"].iloc[-1]),
        "cadence_seconds": 15,
        "anomaly_fraction": round(float((df["scenario_id"] > 0).mean()), 4),
        "anomaly_counts_by_scenario": {
            sc_names.get(int(k), str(k)): int(v)
            for k, v in df[df["scenario_id"] > 0]["scenario_id"].value_counts().to_dict().items()
        },
        "columns": [c for c in df.columns if c != "timestamp"],
        "note": (
            "Wide-format pre-processed parquet. All rate columns already in /sec units — "
            "use prepare_dataset.py --source=synthetic to skip CSV pivot and counter diff."
        ),
    }
    (DATA_DIR / "synthetic_profile.json").write_text(json.dumps(profile, indent=2))
    log.info("saved synthetic_profile.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate synthetic 1-year UPF dataset")
    parser.add_argument("--start", default="2026-01-01",
                        help="Start date YYYY-MM-DD (UTC, default: 2026-01-01)")
    parser.add_argument("--days", type=int, default=365,
                        help="Days to generate (default: 365)")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed")
    args = parser.parse_args()
    df = generate(start_date=args.start, days=args.days, seed=args.seed)
    save(df)
    log.info("done — next: python train/prepare_dataset.py --source=synthetic")
