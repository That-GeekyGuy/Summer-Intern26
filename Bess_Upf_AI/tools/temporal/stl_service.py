"""
STL decomposition sidecar.

Decomposes UPF time series into trend + seasonal + residual components.
Feeds:
  - MOMENT: cleaner residual input (instead of raw rate-converted values)
  - Tier 1: seasonal-deviation rule (deviation_from_seasonal > 3.5σ)
  - Brain 2: regime context (what's expected at this hour vs what's happening)
  - Frontend Insights page: hot zone map, multi-horizon calendar, regime badges

DATA REALITY (updated at startup from actual data):
  - Default dataset: 1.71 days (41 hours) — 3 calendar dates
  - STL period: 96 steps (96 × 15s = 24h, intraday seasonality only)
  - Weekly STL (MSTL with periods=[96, 96×7]) requires ≥ 14 days of data
  - Regime baseline from < 3 days will be noisy — surfaced via data_coverage_days
  - All API responses include data_coverage_days so the frontend can warn users

Endpoints:
  POST /decompose              — decompose a time series segment
  GET  /profile/{channel}     — hourly seasonal profile for a channel
  GET  /hotzone               — 24-hour regime forecast (cached 1h)
  GET  /analysis              — comprehensive temporal analysis package (cached 60s)
  POST /refit                 — trigger background STL refit from source data
  GET  /health                — service status

Environment:
  CSV_PATH                    path to raw data CSV (default: /data/upf_dataset.csv)
  MODELS_DIR                  path to models directory (default: /models)
  STL_PERIOD                  seasonal period in steps (default: 96 = 24h at 15s)
  STL_REFIT_INTERVAL_HOURS    hours between automatic refits (default: 24)
  VM_URL                      VictoriaMetrics URL (fallback data source)
  VM_AUTH_USERNAME            VM basic-auth username
  VM_AUTH_PASSWORD            VM basic-auth password
  HOLIDAY_COUNTRY             ISO country for holiday calendar (default: IN)
  HOLIDAY_SUBDIVISION         subdivision code (optional)
  HOST                        bind address (default: 0.0.0.0)
  PORT                        bind port (default: 8085)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import uvicorn
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel

from calendar_context import get_calendar_context
from regime_classifier import (
    REGIME_CHANNELS, classify_regime, build_hourly_forecast,
    find_peak_trough_hours, minutes_to_next_event,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

CSV_PATH           = Path(os.getenv("CSV_PATH",    "/data/upf_dataset.csv"))
MODELS_DIR         = Path(os.getenv("MODELS_DIR",  "/models"))
STL_PERIOD         = int(os.getenv("STL_PERIOD",   "96"))   # 96 × 15s = 24h
REFIT_INTERVAL_H   = float(os.getenv("STL_REFIT_INTERVAL_HOURS", "24"))
VM_URL             = os.getenv("VM_URL", "http://victoriametrics:8428")
VM_AUTH_USER       = os.getenv("VM_AUTH_USERNAME", "")
VM_AUTH_PASS       = os.getenv("VM_AUTH_PASSWORD", "")
HOST               = os.getenv("HOST", "0.0.0.0")
PORT               = int(os.getenv("PORT", "8085"))

# STL channels that follow diurnal patterns
STL_TRAFFIC_CHANNELS = {
    "port_bytes_N3_rx_rate":       "port_bytes_count{dir=rx_iface=N3",
    "port_bytes_N6_tx_rate":       "port_bytes_count{dir=tx_iface=N6",
    "port_pkts_N3_rx_rate":        "port_packets_count{dir=rx_iface=N3",
    "port_dropped_N3_rx_rate":     "port_dropped_count{dir=rx_iface=N3",
    "port_dropped_N6_rx_rate":     "port_dropped_count{dir=rx_iface=N6",
    "pfcp_sessions_total":         "pfcp_sessions_total{",
    "drop_rate_percentage":        "drop_rate_percentage",
    "tsi_value":                   "tsi_value{",
    "dl_forwarding_efficiency":    "dl_forwarding_efficiency",
}


def is_counter(col: str) -> bool:
    for sub in ("port_bytes_count", "port_packets_count", "port_dropped_count",
                "pfcp_messages_total", "pfcp_sessions_total"):
        if sub in col:
            return True
    return False


# ── Application state ─────────────────────────────────────────────────────────

class _State:
    ready:             bool                      = False
    error_msg:         str                       = ""
    profiles:          dict[str, dict]           = {}
    stl_results:       dict[str, object]         = {}
    data_coverage_days: float                    = 0.0
    period:            int                       = STL_PERIOD
    fit_time:          Optional[datetime]        = None
    # 60s cache for /analysis, 1h cache for /hotzone
    _analysis_cache:   Optional[dict]            = None
    _analysis_cache_ts: float                    = 0.0
    _hotzone_cache:    Optional[dict]            = None
    _hotzone_cache_ts: float                     = 0.0
    _refit_lock:       threading.Lock            = threading.Lock()

_state = _State()
app = FastAPI(title="STL Temporal Sidecar", docs_url=None, redoc_url=None)


# ── STL fitting ───────────────────────────────────────────────────────────────

_LONG_LABEL_COLS = [
    "code", "dir", "direction", "iface", "instance", "job", "le",
    "message_type", "node_id", "quantile", "reason", "result", "sliceid", "version",
]


_STL_METRIC_PREFIXES = {
    "port_bytes_count", "port_packets_count", "port_dropped_count",
    "pfcp_sessions_total", "drop_rate_percentage", "tsi_value",
    "dl_forwarding_efficiency",
}


def _pivot_long_to_wide(df: pd.DataFrame) -> pd.DataFrame:
    """Convert Prometheus long-format export (pre-filtered) to wide format."""
    present = [c for c in _LONG_LABEL_COLS if c in df.columns]
    df = df.copy()
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    combined = pd.Series("", index=df.index, dtype=str)
    for col in present:
        s = df[col].astype(str)
        valid = df[col].notna() & (s != "nan") & (s.str.strip() != "")
        part = (col + "=" + s).where(valid, "")
        has_combined = combined.str.len() > 0
        has_part = part.str.len() > 0
        combined = combined.where(~(has_combined & has_part), combined + "_" + part)
        combined = combined.where(~((~has_combined) & has_part), part)
    df["_series_key"] = (
        df["metric_name"].astype(str)
        + np.where(combined.str.len() > 0, "{" + combined + "}", "")
    )
    wide = df[["timestamp", "_series_key", "value"]].pivot_table(
        index="timestamp", columns="_series_key", values="value", aggfunc="mean"
    )
    wide.columns.name = None
    return wide  # index is already DatetimeIndex


def _load_csv_data() -> Optional[pd.DataFrame]:
    """Load and preprocess CSV data into a 15s-resampled DataFrame."""
    path = CSV_PATH
    if not path.exists():
        alt = Path("/data/prometheus_full_export_20260622_115327.csv")
        if alt.exists():
            path = alt
    if not path.exists():
        return None

    log.info("loading CSV from %s", path)
    # Peek at first row to detect format without loading everything
    header_df = pd.read_csv(str(path), nrows=1, low_memory=False, on_bad_lines="skip")

    if "metric_name" in header_df.columns:
        # Long format — read in chunks to avoid OOM, filter to STL metrics only
        chunks = []
        for chunk in pd.read_csv(str(path), chunksize=200_000, low_memory=False, on_bad_lines="skip"):
            filtered = chunk[chunk["metric_name"].isin(_STL_METRIC_PREFIXES)]
            if not filtered.empty:
                chunks.append(filtered)
        if not chunks:
            log.error("STL: no matching metrics found in CSV")
            return None
        df = pd.concat(chunks, ignore_index=True)
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
        log.info("long-format CSV: kept %d rows for %d STL metrics", len(df), df["metric_name"].nunique())
        df = _pivot_long_to_wide(df)
        log.info("pivot complete: %d columns", len(df.columns))
    else:
        # Wide format: one column per series
        df = pd.read_csv(str(path), low_memory=False, on_bad_lines="skip")
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.sort_values("timestamp").set_index("timestamp")

    return df


def _find_col(df: pd.DataFrame, substring: str) -> Optional[str]:
    for col in df.columns:
        if substring in col:
            return col
    return None


def _extract_series(df: pd.DataFrame) -> dict[str, pd.Series]:
    """Extract and rate-convert each STL channel into 15s-cadence series."""
    series_dict = {}
    for logical, substring in STL_TRAFFIC_CHANNELS.items():
        col = _find_col(df, substring)
        if col is None:
            log.warning("STL: channel '%s' not found (sub='%s')", logical, substring)
            continue
        raw = df[col]
        if is_counter(col):
            # Compute rate at native cadence to handle sub-15s scrape gaps correctly
            native = raw.dropna().sort_index()
            if len(native) < 2:
                continue
            dt_s = native.index.to_series().diff().dt.total_seconds()
            rate_native = (native.diff().clip(lower=0) / dt_s).replace([np.inf, -np.inf], np.nan).dropna()
            series_dict[logical] = rate_native.resample("15s").mean().dropna()
        else:
            series_dict[logical] = raw.resample("15s").mean().dropna()

    return series_dict


def _fit_stl_for_series(name: str, series: pd.Series, period: int) -> Optional[dict]:
    """
    Fit STL to a single channel. Returns profile dict or None on failure.

    Period selection:
      - period=96  →  24h at 15s cadence (intraday seasonality)
      - Weekly seasonality (period=96×7) requires ≥ 14 days of data — skip if unavailable

    With < 2 diurnal cycles, the STL model will fit but hourly estimates are noisy.
    This is acceptable — we flag it via data_coverage_days.
    """
    try:
        from statsmodels.tsa.seasonal import STL
    except ImportError:
        log.error("statsmodels not installed — STL unavailable")
        return None

    n = len(series)
    if n < period * 2:
        log.warning("STL: %s has only %d points (need ≥ %d = 2×period) — skipping", name, n, period * 2)
        return None

    try:
        stl = STL(
            series,
            period=period,
            seasonal=7,     # seasonal smoother window (must be odd, ≥ 7)
            robust=True,    # robust to outliers — important for anomaly data
        )
        result = stl.fit()
    except Exception as e:
        log.warning("STL fit failed for %s: %s", name, e)
        return None

    # Build hourly profile from seasonal component
    # Map each 15s step to its hour-of-day bin
    if hasattr(series.index, "hour"):
        hours = series.index.hour
    else:
        hours = pd.DatetimeIndex(series.index).hour

    hourly_means = {}
    hourly_stds  = {}
    hourly_anomaly_rates = {}

    for h in range(24):
        mask = (hours == h)
        if mask.sum() == 0:
            continue
        result.seasonal[mask]
        raw_vals = series.values[mask]
        residuals = result.resid[mask]

        hourly_means[str(h)]  = float(np.nanmean(raw_vals))
        hourly_stds[str(h)]   = float(np.nanstd(raw_vals)) if len(raw_vals) > 1 else 1.0
        # Anomaly rate: fraction of residuals > 3σ of the channel's overall residual std
        resid_std = float(np.nanstd(result.resid)) + 1e-9
        anomaly_mask = np.abs(residuals) > 3.0 * resid_std
        hourly_anomaly_rates[str(h)] = float(anomaly_mask.mean())

    # Seasonal profile (full 96-step pattern, averaged across available cycles)
    seasonal_values = result.seasonal.values
    n_steps = len(seasonal_values)
    n_cycles = n_steps // period
    if n_cycles >= 1:
        tail = n_cycles * period
        seasonal_matrix = seasonal_values[-tail:].reshape(n_cycles, period)
        seasonal_profile = seasonal_matrix.mean(axis=0).tolist()
    else:
        seasonal_profile = seasonal_values[:period].tolist()

    # Residual statistics for anomaly scoring
    resid = result.resid.values
    resid_mean = float(np.nanmean(resid))
    resid_std  = float(np.nanstd(resid)) if len(resid) > 1 else 1.0

    # Trend slope (last 20% of trend component)
    trend = result.trend.values
    last_chunk = trend[-max(1, len(trend) // 5):]
    trend_slope = float(np.polyfit(np.arange(len(last_chunk)), last_chunk, 1)[0]) if len(last_chunk) > 1 else 0.0

    return {
        "channel":              name,
        "n_points":             n,
        "period":               period,
        "hourly_means":         hourly_means,
        "hourly_stds":          hourly_stds,
        "hourly_anomaly_rates": hourly_anomaly_rates,
        "seasonal_profile":     seasonal_profile,
        "residual_mean":        resid_mean,
        "residual_std":         resid_std,
        "trend_slope":          trend_slope,
        "_result":              result,  # internal, not JSON-serialized
    }


def _fit_all(df: Optional[pd.DataFrame] = None) -> bool:
    """Fit STL for all channels. Returns True on success."""
    with _state._refit_lock:
        _state.ready = False

        if df is None:
            df = _load_csv_data()

        if df is None:
            log.error("STL: no data source available — CSV not found and VM fallback not implemented")
            _state.error_msg = "no data source available"
            return False

        series_dict = _extract_series(df)
        if not series_dict:
            log.error("STL: no series extracted from data")
            _state.error_msg = "no series extracted"
            return False

        # Coverage stats
        all_min = min(s.index.min() for s in series_dict.values() if len(s) > 0)
        all_max = max(s.index.max() for s in series_dict.values() if len(s) > 0)
        coverage_hours = (all_max - all_min).total_seconds() / 3600
        coverage_days  = coverage_hours / 24
        _state.data_coverage_days = round(coverage_days, 2)

        log.info("STL: fitting on %.2f days of data (%d channels)", coverage_days, len(series_dict))
        if coverage_days < 3:
            log.warning("STL: only %.1f days of data — hourly estimates will be noisy", coverage_days)

        # Determine period: intraday only (weekly requires ≥ 14 days)
        period = STL_PERIOD  # 96 for 24h at 15s
        _state.period = period

        profiles = {}
        for name, series in series_dict.items():
            log.info("  fitting STL for %s (%d points) ...", name, len(series))
            profile = _fit_stl_for_series(name, series, period)
            if profile is not None:
                profiles[name] = profile
                log.info("    OK — trend_slope=%.4g residual_std=%.4f", profile["trend_slope"], profile["residual_std"])
            else:
                log.warning("    skipped (too few points or fit failed)")

        _state.profiles = profiles
        _state.fit_time = datetime.now(timezone.utc)

        # Save serializable profiles (without _result)
        serializable = {}
        for name, p in profiles.items():
            serializable[name] = {k: v for k, v in p.items() if k != "_result"}
        save_path = MODELS_DIR / "stl_profiles.json"
        try:
            MODELS_DIR.mkdir(parents=True, exist_ok=True)
            save_path.write_text(json.dumps({
                "generated_at":       _state.fit_time.isoformat(),
                "data_coverage_days": _state.data_coverage_days,
                "period":             period,
                "channels":           serializable,
            }, indent=2))
            log.info("STL profiles saved to %s", save_path)
        except Exception as e:
            log.warning("could not save STL profiles: %s", e)

        _state.ready = True
        # Invalidate caches
        _state._analysis_cache = None
        _state._hotzone_cache = None
        log.info("STL: ready — %d channels fitted", len(profiles))
        return True


def _try_load_precomputed() -> bool:
    """Try to load precomputed STL profiles from models/stl_profiles.json."""
    save_path = MODELS_DIR / "stl_profiles.json"
    if not save_path.exists():
        return False
    try:
        data = json.loads(save_path.read_text())
        channels = data.get("channels", {})
        if not channels:
            return False
        _state.profiles = channels
        _state.data_coverage_days = float(data.get("data_coverage_days", 0.0))
        _state.period = int(data.get("period", STL_PERIOD))
        _state.fit_time = datetime.fromisoformat(data["generated_at"]) if "generated_at" in data else None
        _state.ready = True
        log.info("STL: loaded precomputed profiles (%d channels, %.2f days coverage)",
                 len(channels), _state.data_coverage_days)
        return True
    except Exception as e:
        log.warning("could not load precomputed STL profiles: %s", e)
        return False


# ── Background refit scheduler ────────────────────────────────────────────────

def _background_refit_loop():
    interval = REFIT_INTERVAL_H * 3600
    while True:
        time.sleep(interval)
        log.info("STL: scheduled refit starting")
        _fit_all()


# ── Request / response schemas ────────────────────────────────────────────────

class DecomposeRequest(BaseModel):
    channel: str
    values: list[float]            # time series values (15s cadence)
    timestamps: list[str]          # ISO timestamp strings


class ProfileResponse(BaseModel):
    channel: str
    hourly_means: dict[str, float]
    hourly_stds: dict[str, float]
    peak_hours: list[int]
    trough_hours: list[int]
    data_coverage_days: float
    residual_std: float
    warning: Optional[str] = None


# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
def startup():
    # 1. Try precomputed profiles
    if _try_load_precomputed():
        log.info("STL: using precomputed profiles")
    else:
        # 2. Fit from CSV
        log.info("STL: no precomputed profiles — fitting from CSV")
        _fit_all()

    # Schedule background refit
    t = threading.Thread(target=_background_refit_loop, daemon=True)
    t.start()


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/decompose")
def decompose(req: DecomposeRequest):
    if not _state.ready:
        raise HTTPException(503, detail=f"STL not ready: {_state.error_msg}")

    profile = _state.profiles.get(req.channel)
    if profile is None:
        raise HTTPException(404, detail=f"no STL profile for channel '{req.channel}'")

    values = np.array(req.values, dtype=float)
    n = len(values)

    # Quick reconstruction from seasonal profile
    period = profile["period"]
    seasonal_arr = np.array(profile["seasonal_profile"])

    # Align seasonal component to the request's time window
    if req.timestamps:
        try:
            ts0 = datetime.fromisoformat(req.timestamps[0].replace("Z", "+00:00"))
            # Step index within the day
            start_step = (ts0.hour * 3600 + ts0.minute * 60 + ts0.second) // 15
        except Exception:
            start_step = 0
    else:
        start_step = 0

    seasonal = np.array([
        seasonal_arr[(start_step + i) % period] for i in range(n)
    ])

    # Simple trend: linear fit to values
    if n >= 2:
        x = np.arange(n, dtype=float)
        coeffs = np.polyfit(x, np.nan_to_num(values), 1)
        trend = np.polyval(coeffs, x)
    else:
        trend = values.copy()

    residual = values - trend - seasonal

    # Seasonal deviation for current point
    hour = datetime.fromisoformat(req.timestamps[-1].replace("Z", "+00:00")).hour if req.timestamps else 0
    h_key = str(hour)
    expected_mean = profile["hourly_means"].get(h_key, 0.0)
    expected_std  = max(profile["hourly_stds"].get(h_key, 1.0), 1e-9)
    deviation_from_seasonal = float((values[-1] - expected_mean) / expected_std) if n > 0 else 0.0

    residual_zscore = (residual / (profile["residual_std"] + 1e-9)).tolist()

    return {
        "channel":                  req.channel,
        "n_points":                 n,
        "trend":                    trend.tolist(),
        "seasonal":                 seasonal.tolist(),
        "residual":                 residual.tolist(),
        "residual_zscore":          residual_zscore,
        "seasonal_expected":        expected_mean,
        "seasonal_expected_std":    expected_std,
        "deviation_from_seasonal":  round(deviation_from_seasonal, 3),
        "data_coverage_days":       _state.data_coverage_days,
    }


@app.get("/profile/{channel}")
def profile(channel: str):
    if not _state.ready:
        raise HTTPException(503, detail=f"STL not ready: {_state.error_msg}")

    p = _state.profiles.get(channel)
    if p is None:
        raise HTTPException(404, detail=f"no STL profile for channel '{channel}'")

    # Identify peak and trough hours from hourly means
    means = p["hourly_means"]
    if means:
        mean_vals = [(int(h), v) for h, v in means.items()]
        sorted_by_val = sorted(mean_vals, key=lambda x: x[1], reverse=True)
        peak_hours   = [h for h, _ in sorted_by_val[:6]]
        trough_hours = [h for h, _ in sorted_by_val[-4:]]
    else:
        peak_hours   = []
        trough_hours = []

    warning = None
    if _state.data_coverage_days < 3:
        warning = (
            f"Hourly profile built from only {_state.data_coverage_days:.1f} days of data. "
            "Each hour appears 1-2 times — estimates are noisy. "
            "Accuracy improves with ≥ 7 days of data."
        )

    return ProfileResponse(
        channel=channel,
        hourly_means={k: round(v, 4) for k, v in p["hourly_means"].items()},
        hourly_stds={k: round(v, 4) for k, v in p["hourly_stds"].items()},
        peak_hours=sorted(peak_hours),
        trough_hours=sorted(trough_hours),
        data_coverage_days=_state.data_coverage_days,
        residual_std=round(p["residual_std"], 6),
        warning=warning,
    )


@app.get("/hotzone")
def hotzone():
    """24-hour regime forecast. Cached for 1 hour."""
    now = time.time()
    if _state._hotzone_cache and (now - _state._hotzone_cache_ts) < 3600:
        return _state._hotzone_cache

    if not _state.ready:
        return {"available": False, "error": _state.error_msg}

    hourly = build_hourly_forecast(_state.profiles, _state.data_coverage_days)
    peak_hours, trough_hours = find_peak_trough_hours(hourly)
    current_hour = datetime.now(timezone.utc).hour
    next_peak_min   = minutes_to_next_event(current_hour, peak_hours)
    next_trough_min = minutes_to_next_event(current_hour, trough_hours)

    result = {
        "generated_at":          datetime.now(timezone.utc).isoformat(),
        "data_coverage_days":    _state.data_coverage_days,
        "hourly_forecast":       hourly,
        "peak_hours":            sorted(peak_hours),
        "trough_hours":          sorted(trough_hours),
        "next_peak_in_minutes":  next_peak_min,
        "next_trough_in_minutes": next_trough_min,
        "warning": (
            f"Hot zone forecast built from {_state.data_coverage_days:.1f} days of data. "
            "Regime labels are approximate. Accuracy improves with ≥ 7 days."
        ) if _state.data_coverage_days < 7 else None,
    }

    _state._hotzone_cache = result
    _state._hotzone_cache_ts = now
    return result


@app.get("/analysis")
def analysis():
    """Comprehensive temporal analysis package. Cached for 60 seconds."""
    now = time.time()
    if _state._analysis_cache and (now - _state._analysis_cache_ts) < 60:
        return _state._analysis_cache

    if not _state.ready:
        return {"available": False, "error": _state.error_msg}

    now_ts = datetime.now(timezone.utc)
    cal = get_calendar_context(now_ts)

    # Current state regime — use latest available values from profiles as proxy
    # (actual live values would come from the caller; we estimate from profile means)
    current_hour = now_ts.hour
    session_profile = _state.profiles.get("pfcp_sessions_total", {})
    hourly_means = session_profile.get("hourly_means", {})
    current_estimated_sessions = hourly_means.get(str(current_hour), 0.0)

    # Estimate current features from profile means for this hour
    current_features = {}
    for ch in REGIME_CHANNELS:
        p = _state.profiles.get(ch, {})
        mean_val = p.get("hourly_means", {}).get(str(current_hour), 0.0)
        current_features[ch] = mean_val

    regime, regime_pct = classify_regime(
        current_features, _state.profiles, current_hour,
        is_holiday=cal["is_holiday"], is_weekend=cal["is_weekend"],
    )

    # Deviation from seasonal for sessions
    h_key = str(current_hour)
    session_profile.get("hourly_means", {}).get(h_key, 0.0)
    session_profile.get("hourly_stds",  {}).get(h_key, 1.0)

    # Hot zone forecast
    hz = hotzone()
    hourly_profile = hz.get("hourly_forecast", [])
    peak_hours     = hz.get("peak_hours", [])
    trough_hours   = hz.get("trough_hours", [])

    result = {
        "generated_at":       now_ts.isoformat(),
        "data_coverage_days": _state.data_coverage_days,
        "stl_period_steps":   _state.period,
        "stl_fit_time":       _state.fit_time.isoformat() if _state.fit_time else None,
        "current_state": {
            "regime":                  regime,
            "percentile":              round(regime_pct, 3),
            "calendar":                cal,
            "deviation_from_seasonal": None,  # populated with live values
            "within_expected_range":   regime in ("low", "normal"),
            "estimated_sessions":      round(current_estimated_sessions, 0),
        },
        "hourly_profile_today":   hourly_profile,
        "hot_zones_next_24h": {
            "peak_hours":            sorted(peak_hours),
            "trough_hours":          sorted(trough_hours),
            "next_peak_in_minutes":  hz.get("next_peak_in_minutes"),
            "next_trough_in_minutes": hz.get("next_trough_in_minutes"),
        },
        "available_channels": sorted(_state.profiles.keys()),
        "warning": (
            f"Temporal analysis built from {_state.data_coverage_days:.1f} days of data "
            "(< 3 days). Regime labels and hourly baselines are approximate. "
            "Reliability improves significantly with ≥ 7 days of historical data."
        ) if _state.data_coverage_days < 3 else (
            f"Temporal analysis built from {_state.data_coverage_days:.1f} days of data. "
            "Weekly patterns not available (need ≥ 14 days)."
        ) if _state.data_coverage_days < 14 else None,
    }

    _state._analysis_cache = result
    _state._analysis_cache_ts = now
    return result


@app.post("/refit")
async def refit(background_tasks: BackgroundTasks):
    """Trigger a background STL refit. Returns immediately."""
    if _state._refit_lock.locked():
        return {"status": "already_running"}
    background_tasks.add_task(_fit_all)
    return {"status": "refit_started"}


@app.get("/health")
def health():
    return {
        "available":          _state.ready,
        "data_coverage_days": _state.data_coverage_days,
        "n_channels_fitted":  len(_state.profiles),
        "stl_period":         _state.period,
        "fit_time":           _state.fit_time.isoformat() if _state.fit_time else None,
        "error":              _state.error_msg if not _state.ready else None,
        "warning":            (
            f"Only {_state.data_coverage_days:.1f} days of data — profiles are noisy"
        ) if _state.data_coverage_days < 3 else None,
    }


if __name__ == "__main__":
    uvicorn.run("stl_service:app", host=HOST, port=PORT, log_level="info")
