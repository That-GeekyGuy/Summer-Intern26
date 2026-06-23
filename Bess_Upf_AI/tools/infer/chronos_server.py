"""
Chronos-2 forecasting sidecar.

Internal-network only — never exposed through Caddy.
Loads ChronosPipeline at startup.
Starts in degraded mode on load failure — never crashes.

Endpoints:
  POST /forecast — generate probabilistic forecast for uoi_value
  GET  /health   — model load status, context length, forecast horizon

Horizons (pass as `horizon` field in request body):
  "short"  — 20 steps  =   5 min  (direct Chronos-2)
  "medium" — 240 steps =   1 hour (Chronos + linear extrapolation)
  "long"   — 1440 steps =  6 hours (hybrid: STL seasonal baseline + Chronos trend)

Environment:
  MODEL_PATH        path to fine-tuned checkpoint dir       (default: /models/chronos)
  METADATA_PATH     path to chronos_metadata.json           (default: /models/chronos_metadata.json)
  STL_PROFILES_PATH path to stl_profiles.json from stl-sidecar (default: /models/stl_profiles.json)
  HOST              bind address                             (default: 0.0.0.0)
  PORT              bind port                                (default: 8084)
"""
from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import torch
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

MODEL_PATH        = Path(os.getenv("MODEL_PATH",        "/models/chronos"))
METADATA_PATH     = Path(os.getenv("METADATA_PATH",     "/models/chronos_metadata.json"))
STL_PROFILES_PATH = Path(os.getenv("STL_PROFILES_PATH", "/models/stl_profiles.json"))
HOST              = os.getenv("HOST", "0.0.0.0")
PORT              = int(os.getenv("PORT", "8084"))

# Chronos-t5-small has a practical prediction limit of ~64 steps.
# Beyond that we switch to extrapolation + STL hybrid.
CHRONOS_MAX_STEPS = 64

HORIZON_STEPS = {
    "short":  20,    # 5 min
    "medium": 240,   # 1 hour
    "long":   1440,  # 6 hours
}

# STL seasonal profiles loaded lazily at first long-horizon request.
_stl_profiles: dict | None = None
_stl_profiles_loaded: bool = False


def _load_stl_profiles() -> dict:
    global _stl_profiles, _stl_profiles_loaded
    if _stl_profiles_loaded:
        return _stl_profiles or {}
    _stl_profiles_loaded = True
    if STL_PROFILES_PATH.exists():
        try:
            import json as _json
            _stl_profiles = _json.loads(STL_PROFILES_PATH.read_text())
            log.info("STL profiles loaded: %d channels", len(_stl_profiles))
        except Exception as e:
            log.warning("STL profiles load failed: %s", e)
    else:
        log.info("STL profiles not found at %s — long-horizon uses trend-only", STL_PROFILES_PATH)
    return _stl_profiles or {}

app = FastAPI(title="Chronos-2 Forecasting Sidecar", docs_url=None, redoc_url=None)


# ── State ─────────────────────────────────────────────────────────────────────

class _State:
    available: bool          = False
    error_msg: str           = ""
    pipeline: Any | None     = None
    context_length: int      = 512
    forecast_horizon: int    = 20
    resample_seconds: int    = 15
    uoi_threshold: float     = 1.0
    model_version: str       = ""


_state = _State()


def _naive_baseline_predict(context: np.ndarray, horizon: int) -> dict:
    """Statistical fallback when Chronos is unavailable."""
    last_val = float(context[-1])
    if len(context) >= 10:
        x = np.arange(10, dtype=float)
        y = context[-10:].astype(float)
        slope = float(np.polyfit(x, y, 1)[0])
    else:
        slope = 0.0
    steps = np.arange(1, horizon + 1, dtype=float)
    median_fc = last_val + slope * steps
    std_est = float(np.std(context[-20:]) if len(context) >= 20 else max(np.std(context), 0.01))
    return {
        "p10": median_fc - 1.28 * std_est,
        "p50": median_fc,
        "p90": median_fc + 1.28 * std_est,
        "samples": np.stack([median_fc + np.random.normal(0, std_est, horizon) for _ in range(20)]),
    }


def _load_models():
    try:
        # Load metadata
        if METADATA_PATH.exists():
            meta = json.loads(METADATA_PATH.read_text())
            _state.context_length    = int(meta.get("context_length",   512))
            _state.forecast_horizon  = int(meta.get("forecast_horizon",  20))
            _state.resample_seconds  = int(meta.get("resample_seconds",  15))
            _state.uoi_threshold     = float(meta.get("uoi_threshold",   1.0))
            log.info("chronos metadata loaded: ctx=%d horizon=%d threshold=%.4f",
                     _state.context_length, _state.forecast_horizon, _state.uoi_threshold)

        # Try to load Chronos pipeline
        try:
            from chronos import ChronosPipeline

            # Check for fine-tuned or zero-shot marker
            zero_shot_marker = MODEL_PATH / "zero_shot_marker.json"
            if zero_shot_marker.exists():
                log.info("zero-shot marker found — loading base model")
                model_id = "amazon/chronos-t5-small"
            elif (MODEL_PATH / "config.json").exists():
                model_id = str(MODEL_PATH)
                log.info("loading fine-tuned checkpoint from %s", model_id)
            else:
                model_id = "amazon/chronos-t5-small"
                log.info("no checkpoint found — loading base model")

            _state.pipeline = ChronosPipeline.from_pretrained(
                model_id,
                device_map="cpu",
                torch_dtype=torch.float32,
            )
            _state.model_version = f"chronos-t5-small@{model_id}"
            log.info("Chronos pipeline loaded from '%s'", model_id)

        except ImportError:
            log.warning("chronos not installed — using naive baseline")
            _state.model_version = "naive-baseline"
        except Exception as e:
            log.warning("Chronos model failed (%s) — using naive baseline", e)
            _state.model_version = "naive-baseline"

        _state.available = True
        log.info("Chronos sidecar ready (ctx=%d, horizon=%d, threshold=%.4f)",
                 _state.context_length, _state.forecast_horizon, _state.uoi_threshold)

    except Exception as e:
        _state.error_msg = str(e)
        log.error("Chronos sidecar load failed: %s", e)


@app.on_event("startup")
def startup():
    _load_models()


# ── Request / response schemas ────────────────────────────────────────────────

class ForecastRequest(BaseModel):
    context: list[float]                     # uoi_value values (last 512 steps)
    timestamps: list[str] | None = None      # ISO timestamps for context (optional)
    horizon: str = "short"                   # "short" | "medium" | "long"


class ForecastResponse(BaseModel):
    available: bool
    forecast_median: list[float] | None = None
    forecast_p10: list[float] | None    = None
    forecast_p90: list[float] | None    = None
    forecast_timestamps: list[str] | None = None
    capacity_breach_eta_minutes: float | None = None
    breach_probability: float | None          = None
    trend: str | None = None                  # "increasing" | "decreasing" | "stable"
    model_version: str = ""
    horizon: str = "short"
    forecast_method: str | None = None        # "direct" | "extrapolation" | "stl_hybrid"
    confidence_note: str | None = None        # human-readable confidence caveat for long horizon


# ── Endpoints ─────────────────────────────────────────────────────────────────

def _run_chronos_short(context: np.ndarray, steps: int) -> dict:
    """Run Chronos-2 for up to CHRONOS_MAX_STEPS. Returns {p10, p50, p90, samples}."""
    capped = min(steps, CHRONOS_MAX_STEPS)
    try:
        if _state.pipeline is not None:
            ctx_tensor = torch.tensor(context, dtype=torch.float32).unsqueeze(0)
            with torch.no_grad():
                pred = _state.pipeline.predict(
                    ctx_tensor,
                    prediction_length=capped,
                    num_samples=20,
                    temperature=1.0,
                    top_k=50,
                    top_p=1.0,
                )
            samples = pred[0].numpy()  # (20, capped)
        else:
            raise RuntimeError("pipeline not loaded")
    except Exception as e:
        log.warning("Chronos predict failed: %s — using baseline", e)
        d = _naive_baseline_predict(context, capped)
        return d
    return {
        "p10":     np.percentile(samples, 10, axis=0),
        "p50":     np.percentile(samples, 50, axis=0),
        "p90":     np.percentile(samples, 90, axis=0),
        "samples": samples,
    }


def _extrapolate(context: np.ndarray, short_p50: np.ndarray, short_std: np.ndarray,
                 total_steps: int) -> dict:
    """
    Extend a short forecast using linear trend extrapolation.
    Uncertainty bands widen proportionally with sqrt(step).
    """
    n_short = len(short_p50)
    # Compute linear slope from the last 10 context values + short forecast
    anchor = np.concatenate([context[-10:], short_p50]) if len(context) >= 10 else short_p50
    x = np.arange(len(anchor), dtype=float)
    slope = float(np.polyfit(x, anchor, 1)[0])

    last_median = float(short_p50[-1])
    base_std = float(short_std[-1]) if len(short_std) else float(context.std() + 1e-6)

    extrap_steps = total_steps - n_short
    extrap_p50 = np.array([last_median + slope * (i + 1) for i in range(extrap_steps)], dtype=np.float32)
    extrap_spread = np.array([base_std * math.sqrt(i + 1) for i in range(extrap_steps)], dtype=np.float32)

    full_p50 = np.concatenate([short_p50, extrap_p50])
    full_p10 = np.concatenate([short_p50 - 1.28 * short_std, extrap_p50 - 1.28 * extrap_spread])
    full_p90 = np.concatenate([short_p50 + 1.28 * short_std, extrap_p50 + 1.28 * extrap_spread])

    # Build synthetic samples for breach probability
    n_samples = 20
    rng = np.random.default_rng()
    samples = np.stack([full_p50 + rng.normal(0, np.concatenate([short_std, extrap_spread])) for _ in range(n_samples)])

    return {"p10": full_p10, "p50": full_p50, "p90": full_p90, "samples": samples}


def _stl_hybrid(context: np.ndarray, short_result: dict, total_steps: int,
                last_ts: datetime, resample_s: int) -> tuple[dict, str]:
    """
    Long-horizon hybrid forecast:
    - Steps 0-19: direct Chronos-2
    - Steps 20+:  STL seasonal baseline for uoi_session_component scaled by Chronos slope
    Returns (forecast_dict, confidence_note).
    """
    profiles = _load_stl_profiles()
    n_short = len(short_result["p50"])

    # Chronos-based slope (per-step) from the short segment
    if n_short >= 2:
        slope_per_step = float(short_result["p50"][-1] - short_result["p50"][0]) / max(n_short - 1, 1)
    else:
        slope_per_step = 0.0

    last_short_val = float(short_result["p50"][-1])

    # Build the extended portion using STL seasonal uoi profile (or linear if unavailable)
    extrap_p50 = []
    extrap_spread = []
    uoi_profile = profiles.get("uoi_session_component") or profiles.get("pfcp_sessions_total")

    for i in range(total_steps - n_short):
        ts_step = last_ts + timedelta(seconds=resample_s * (n_short + i + 1))
        hour = ts_step.hour

        if uoi_profile:
            hourly_means = uoi_profile.get("hourly_means", {})
            hourly_stds  = uoi_profile.get("hourly_stds", {})
            h_key = str(hour)
            seasonal_mean = float(hourly_means.get(h_key, 0.0))
            seasonal_std  = float(hourly_stds.get(h_key, max(abs(seasonal_mean) * 0.15, 0.01)))
            # Blend: seasonal mean + Chronos trend correction
            pred_val = seasonal_mean + slope_per_step * (n_short + i)
        else:
            # No STL profile: pure linear extrapolation
            base_std = float(context.std() + 1e-6)
            pred_val = last_short_val + slope_per_step * (i + 1)
            seasonal_std = base_std * math.sqrt(i + 1)

        extrap_p50.append(pred_val)
        extrap_spread.append(seasonal_std)

    extrap_p50 = np.array(extrap_p50, dtype=np.float32)
    extrap_spread = np.array(extrap_spread, dtype=np.float32)

    full_p50 = np.concatenate([short_result["p50"], extrap_p50])
    full_p10 = np.concatenate([
        short_result["p10"],
        extrap_p50 - 1.28 * extrap_spread,
    ])
    full_p90 = np.concatenate([
        short_result["p90"],
        extrap_p50 + 1.28 * extrap_spread,
    ])

    rng = np.random.default_rng()
    samples = np.stack([full_p50 + rng.normal(0, np.concatenate([
        (short_result["p90"] - short_result["p10"]) / 2.56,
        extrap_spread,
    ])) for _ in range(20)])

    method = "stl_hybrid" if uoi_profile else "trend_extrapolation"
    note = (
        "6h forecast uses STL seasonal baseline + Chronos-2 near-term trend correction. "
        "Uncertainty grows with forecast horizon. Only the first 5 min (20 steps) "
        "are direct model output; remainder is trend-projected."
    )

    return {"p10": full_p10, "p50": full_p50, "p90": full_p90, "samples": samples}, note


@app.post("/forecast", response_model=ForecastResponse)
def forecast(req: ForecastRequest):
    if not _state.available:
        return ForecastResponse(available=False, model_version="degraded", horizon=req.horizon)

    context = np.array(req.context, dtype=np.float32)
    if len(context) < 10:
        return ForecastResponse(
            available=True, trend="unknown",
            model_version=_state.model_version, horizon=req.horizon,
        )

    context = context[-_state.context_length:]

    horizon_key   = req.horizon if req.horizon in HORIZON_STEPS else "short"
    total_steps   = HORIZON_STEPS[horizon_key]
    forecast_method = "direct"
    confidence_note: str | None = None

    # ── Run forecast by horizon ───────────────────────────────────────────────
    if req.timestamps and len(req.timestamps) > 0:
        try:
            last_ts = datetime.fromisoformat(req.timestamps[-1].replace("Z", "+00:00"))
        except Exception:
            last_ts = datetime.now(timezone.utc)
    else:
        last_ts = datetime.now(timezone.utc)

    if horizon_key == "short":
        short = _naive_baseline_predict(context, total_steps) if _state.pipeline is None \
            else _run_chronos_short(context, total_steps)
        p10, p50, p90, samples = short["p10"], short["p50"], short["p90"], short["samples"]
        forecast_method = "direct"

    elif horizon_key == "medium":
        short = _run_chronos_short(context, CHRONOS_MAX_STEPS)
        short_std = (short["p90"] - short["p10"]) / 2.56
        ext = _extrapolate(context, short["p50"], short_std, total_steps)
        p10, p50, p90, samples = ext["p10"], ext["p50"], ext["p90"], ext["samples"]
        forecast_method = "extrapolation"
        confidence_note = (
            "1h forecast uses direct Chronos-2 for first 16 min, then linear trend extrapolation "
            "with widening uncertainty bands."
        )

    else:  # long
        short = _run_chronos_short(context, CHRONOS_MAX_STEPS)
        hybrid, confidence_note = _stl_hybrid(context, short, total_steps, last_ts, _state.resample_seconds)
        p10, p50, p90, samples = hybrid["p10"], hybrid["p50"], hybrid["p90"], hybrid["samples"]
        forecast_method = "stl_hybrid"

    # ── Breach detection ──────────────────────────────────────────────────────
    thr = _state.uoi_threshold
    breach_steps = np.where(p50 > thr)[0]
    eta_min = float(breach_steps[0] * _state.resample_seconds / 60.0) if len(breach_steps) > 0 else None
    breach_prob = float((samples > thr).any(axis=-1).mean())

    # ── Trend ─────────────────────────────────────────────────────────────────
    recent_mean = float(context[-min(20, len(context)):].mean())
    fc_mean     = float(p50[:min(20, len(p50))].mean())
    rel_change  = (fc_mean - recent_mean) / (abs(recent_mean) + 1e-6)
    if rel_change > 0.05:
        trend = "increasing"
    elif rel_change < -0.05:
        trend = "decreasing"
    else:
        trend = "stable"

    fc_timestamps = [
        (last_ts + timedelta(seconds=_state.resample_seconds * (i + 1))).isoformat()
        for i in range(total_steps)
    ]

    return ForecastResponse(
        available=True,
        forecast_median=[round(float(v), 4) for v in p50],
        forecast_p10=[round(float(v), 4) for v in p10],
        forecast_p90=[round(float(v), 4) for v in p90],
        forecast_timestamps=fc_timestamps,
        capacity_breach_eta_minutes=round(eta_min, 2) if eta_min is not None else None,
        breach_probability=round(breach_prob, 4),
        trend=trend,
        model_version=_state.model_version,
        horizon=horizon_key,
        forecast_method=forecast_method,
        confidence_note=confidence_note,
    )


@app.get("/health")
def health():
    return {
        "available":       _state.available,
        "model_version":   _state.model_version,
        "context_length":  _state.context_length,
        "forecast_horizon": _state.forecast_horizon,
        "uoi_threshold":   _state.uoi_threshold,
        "error":           _state.error_msg if not _state.available else None,
    }


if __name__ == "__main__":
    uvicorn.run("chronos_server:app", host=HOST, port=PORT, log_level="info")
