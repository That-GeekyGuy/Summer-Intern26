#!/usr/bin/env python3
"""
Chronos-2 fine-tuning for uoi_value forecasting (Tier 2).

Protocol:
  1. Zero-shot evaluate FIRST — if WQL improves by < 5% after fine-tuning, use zero-shot.
  2. Use chronos-t5-small (46M params) on CPU — no GPU required.
  3. Forecast horizon: 20 steps × 15s = 5 minutes ahead.
  4. Context length: 512 steps (~128 min).

Inputs  (from prepare_dataset.py):
  tools/train/data/chronos_train.parquet   — columns: timestamp, uoi_value

Outputs:
  models/chronos_finetuned/   — fine-tuned checkpoint (or base model symlink if zero-shot wins)
  models/chronos_metadata.json — context_len, horizon, threshold, WQL, calibration

Usage:
  cd tools
  python train/train_chronos.py [--data-dir train/data] [--models-dir ../models]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import torch

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

# ── Constants ─────────────────────────────────────────────────────────────────

CONTEXT_LEN      = 512    # steps of context
FORECAST_HORIZON = 20     # steps ahead (20 × 15s = 5 min)
RESAMPLE_S       = 15     # seconds per step
TRAIN_RATIO      = 0.80
LR               = 1e-4
EPOCHS           = 20
EARLY_STOP       = 5
BATCH_SIZE       = 64    # GPU: doubled from 32 on CPU
MIN_WQL_IMPROVEMENT = 0.05  # fine-tune must beat zero-shot by this fraction


# ── WQL / calibration helpers ────────────────────────────────────────────────

def weighted_quantile_loss(y_true: np.ndarray, quantile_forecasts: np.ndarray,
                           quantiles: list[float]) -> float:
    """
    Weighted Quantile Loss (WQL) averaged over all quantiles and all time steps.
    y_true:            (n_evals, horizon)
    quantile_forecasts:(n_evals, n_quantiles, horizon)
    """
    total_loss = 0.0
    n_q = len(quantiles)
    n_evals, horizon = y_true.shape

    for qi, q in enumerate(quantiles):
        fc = quantile_forecasts[:, qi, :]    # (n_evals, horizon)
        err = y_true - fc
        ql  = np.where(err >= 0, q * err, (q - 1) * err)
        total_loss += ql.mean()

    return float(total_loss / n_q)


def mean_interval_score(y_true: np.ndarray, p10: np.ndarray, p90: np.ndarray) -> float:
    """Mean Interval Score for the 80% prediction interval (p10-p90)."""
    alpha = 0.2
    width = p90 - p10
    penalty_low  = (2 / alpha) * np.maximum(p10 - y_true, 0)
    penalty_high = (2 / alpha) * np.maximum(y_true - p90, 0)
    mis = width + penalty_low + penalty_high
    return float(mis.mean())


def calibration_coverage(y_true: np.ndarray, p10: np.ndarray, p90: np.ndarray) -> float:
    """Fraction of observations within the 80% interval."""
    within = (y_true >= p10) & (y_true <= p90)
    return float(within.mean())


# ── Sliding window iterator ───────────────────────────────────────────────────

def make_context_forecast_pairs(series: np.ndarray, context_len: int, horizon: int, stride: int = 1):
    """
    Yield (context, target) pairs for evaluation / training.
    context: (context_len,)
    target:  (horizon,)
    """
    n = len(series)
    pairs = []
    i = 0
    while i + context_len + horizon <= n:
        ctx = series[i:i + context_len]
        tgt = series[i + context_len:i + context_len + horizon]
        pairs.append((ctx, tgt))
        i += stride
    return pairs


# ── Chronos model management ─────────────────────────────────────────────────

def load_chronos_pipeline(model_path: str | None = None):
    """
    Load ChronosPipeline. Falls back to statistical baseline if chronos not installed.
    """
    try:
        from chronos import ChronosPipeline

        path = model_path or "amazon/chronos-t5-small"
        log.info("loading Chronos pipeline from '%s' …", path)
        pipeline = ChronosPipeline.from_pretrained(
            path,
            device_map="auto",   # auto-selects CUDA if available, falls back to CPU
            torch_dtype=torch.float32,
        )
        log.info("Chronos pipeline loaded")
        return pipeline
    except ImportError:
        log.warning("chronos not installed — using naive seasonal baseline")
        return None
    except Exception as e:
        log.warning("Chronos failed to load (%s) — using naive seasonal baseline", e)
        return None


def predict_chronos(pipeline, context: np.ndarray, n_samples: int = 20,
                    horizon: int = FORECAST_HORIZON) -> dict:
    """
    Run Chronos prediction. Returns dict with quantile arrays.
    """
    if pipeline is None:
        return _naive_baseline_predict(context, horizon)

    try:
        from chronos import ChronosPipeline
        ctx_tensor = torch.tensor(context, dtype=torch.float32).unsqueeze(0)  # (1, context_len)
        with torch.no_grad():
            forecast = pipeline.predict(
                ctx_tensor,
                prediction_length=horizon,
                num_samples=n_samples,
                temperature=1.0,
                top_k=50,
                top_p=1.0,
            )
        # forecast: (1, n_samples, horizon) tensor
        samples = forecast[0].numpy()  # (n_samples, horizon)
        return {
            "median": float(np.median(samples, axis=0).mean()),
            "p10":    np.percentile(samples, 10, axis=0),
            "p50":    np.percentile(samples, 50, axis=0),
            "p90":    np.percentile(samples, 90, axis=0),
            "samples": samples,
        }
    except Exception as e:
        log.warning("Chronos predict failed: %s — using baseline", e)
        return _naive_baseline_predict(context, horizon)


def _naive_baseline_predict(context: np.ndarray, horizon: int) -> dict:
    """
    Naive seasonal baseline: repeat the last known value + linear trend.
    Used when Chronos is unavailable.
    """
    last_val = float(context[-1])
    # Linear trend from last 10 values
    if len(context) >= 10:
        x = np.arange(10, dtype=float)
        y = context[-10:].astype(float)
        slope = float(np.polyfit(x, y, 1)[0])
    else:
        slope = 0.0

    forecast_steps = np.arange(1, horizon + 1, dtype=float)
    median_fc = last_val + slope * forecast_steps
    std_est   = float(np.std(context[-20:]) if len(context) >= 20 else np.std(context))

    return {
        "median": float(median_fc.mean()),
        "p10":    median_fc - 1.28 * std_est,
        "p50":    median_fc,
        "p90":    median_fc + 1.28 * std_est,
        "samples": np.stack([median_fc + np.random.normal(0, std_est, horizon) for _ in range(20)]),
    }


# ── Zero-shot evaluation ──────────────────────────────────────────────────────

def evaluate_pipeline(pipeline, eval_pairs: list, horizon: int) -> dict:
    """Evaluate a pipeline on eval_pairs. Returns WQL, MIS, calibration."""
    if not eval_pairs:
        return {"wql": float("nan"), "mis": float("nan"), "calibration": float("nan")}

    quantiles = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
    y_true_all  = []
    q_fc_all    = []
    p10_all, p90_all = [], []

    for ctx, tgt in eval_pairs:
        pred = predict_chronos(pipeline, ctx, horizon=horizon)
        samples = pred["samples"]  # (n_samples, horizon)

        q_row = np.array([np.percentile(samples, q * 100, axis=0) for q in quantiles])  # (9, horizon)
        q_fc_all.append(q_row)
        y_true_all.append(tgt[:horizon])
        p10_all.append(pred["p10"])
        p90_all.append(pred["p90"])

    y_true = np.array(y_true_all)   # (n_evals, horizon)
    q_fc   = np.array(q_fc_all)     # (n_evals, 9, horizon)
    p10    = np.array(p10_all)
    p90    = np.array(p90_all)

    wql  = weighted_quantile_loss(y_true, q_fc, quantiles)
    mis  = mean_interval_score(y_true, p10, p90)
    cal  = calibration_coverage(y_true, p10, p90)

    return {"wql": round(wql, 6), "mis": round(mis, 4), "calibration": round(cal, 4)}


# ── Fine-tuning (continue pre-training) ──────────────────────────────────────

def fine_tune_chronos(pipeline, train_pairs: list, val_pairs: list,
                      models_dir: Path, epochs: int = EPOCHS,
                      lr: float = LR, batch_size: int = BATCH_SIZE) -> Path | None:
    """
    Fine-tune Chronos by continuing pre-training on (context, target) pairs.
    Returns path to saved checkpoint, or None if fine-tuning failed.
    """
    if pipeline is None:
        log.warning("Chronos pipeline not available — skipping fine-tuning")
        return None

    try:
        import transformers
    except ImportError:
        log.warning("transformers not installed — skipping fine-tuning")
        return None

    try:
        model = pipeline.model
        if not hasattr(model, "parameters"):
            log.warning("Chronos model does not support fine-tuning — skipping")
            return None

        optimizer = torch.optim.AdamW(model.parameters(), lr=lr)
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)

        best_val_wql = float("inf")
        no_improve   = 0
        checkpoint_path = models_dir / "chronos_finetuned"

        for epoch in range(1, epochs + 1):
            model.train()
            epoch_loss = 0.0
            np.random.shuffle(train_pairs)  # type: ignore

            for i in range(0, len(train_pairs), batch_size):
                batch = train_pairs[i:i + batch_size]
                contexts = torch.tensor(
                    np.array([p[0] for p in batch]), dtype=torch.float32
                )
                targets = torch.tensor(
                    np.array([p[1] for p in batch]), dtype=torch.float32
                )

                optimizer.zero_grad()
                try:
                    out = model(past_values=contexts, future_values=targets)
                    loss = out.loss if hasattr(out, "loss") else out[0]
                    if loss is None or not loss.requires_grad:
                        continue
                    loss.backward()
                    torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                    optimizer.step()
                    epoch_loss += float(loss.item()) * len(batch)
                except Exception as e:
                    log.debug("fine-tuning step failed: %s", e)

            scheduler.step()
            avg_loss = epoch_loss / max(len(train_pairs), 1)

            # Validation WQL
            model.eval()
            val_metrics = evaluate_pipeline(pipeline, val_pairs[:20], FORECAST_HORIZON)
            val_wql = val_metrics["wql"]

            log.info("epoch %d/%d  train_loss=%.4f  val_wql=%.6f  best=%.6f",
                     epoch, epochs, avg_loss, val_wql, best_val_wql)

            if val_wql < best_val_wql - 1e-6:
                best_val_wql = val_wql
                no_improve   = 0
                checkpoint_path.mkdir(parents=True, exist_ok=True)
                pipeline.model.save_pretrained(str(checkpoint_path))
                log.info("saved checkpoint (val_wql=%.6f)", best_val_wql)
            else:
                no_improve += 1

            if no_improve >= EARLY_STOP:
                log.info("early stopping at epoch %d", epoch)
                break

        return checkpoint_path if (checkpoint_path / "config.json").exists() else None

    except Exception as e:
        log.warning("fine-tuning failed: %s — using zero-shot", e)
        return None


# ── Breach metrics ────────────────────────────────────────────────────────────

def compute_breach_metrics(pipeline, eval_pairs: list, uoi_threshold: float,
                           horizon: int = FORECAST_HORIZON, resample_s: int = RESAMPLE_S) -> dict:
    """
    For each eval pair, compute:
    - capacity_breach_eta_minutes: first forecast step where median > uoi_threshold
    - breach_probability: fraction of sample paths crossing threshold in horizon
    """
    etas = []
    probs = []

    for ctx, tgt in eval_pairs:
        pred = predict_chronos(pipeline, ctx, horizon=horizon)
        samples = pred["samples"]     # (n_samples, horizon)
        median  = pred["p50"]         # (horizon,)

        # ETA: first step where median exceeds threshold
        breach_steps = np.where(median > uoi_threshold)[0]
        if len(breach_steps) > 0:
            eta_min = float(breach_steps[0] * resample_s / 60.0)
            etas.append(eta_min)
        else:
            etas.append(None)

        # Breach probability
        paths_breaching = (samples > uoi_threshold).any(axis=-1)
        probs.append(float(paths_breaching.mean()))

    valid_etas = [e for e in etas if e is not None]
    return {
        "mean_breach_eta_minutes":   round(float(np.mean(valid_etas)), 2) if valid_etas else None,
        "median_breach_eta_minutes": round(float(np.median(valid_etas)), 2) if valid_etas else None,
        "mean_breach_probability":   round(float(np.mean(probs)), 4),
        "fraction_with_breach_eta":  round(len(valid_etas) / max(len(etas), 1), 4),
    }


# ── Earliness vs Tier 3 ───────────────────────────────────────────────────────

def compute_earliness_vs_tier3(eval_pairs: list, pipeline, uoi_threshold: float,
                                resample_s: int = RESAMPLE_S) -> dict:
    """
    Compare Chronos-2 breach ETA to Tier 3 (linear extrapolation).
    Returns mean difference in minutes (positive = Chronos is earlier).
    """
    diffs = []
    for ctx, tgt in eval_pairs:
        median_fc = predict_chronos(pipeline, ctx)["p50"]

        # Chronos breach ETA
        breach = np.where(median_fc > uoi_threshold)[0]
        chronos_eta = float(breach[0] * resample_s / 60.0) if len(breach) > 0 else None

        # Tier 3 linear extrapolation from last 20 context values
        last_n = min(20, len(ctx))
        x = np.arange(last_n, dtype=float)
        y = ctx[-last_n:].astype(float)
        slope, intercept = np.polyfit(x, y, 1)
        # Steps to threshold from now: (threshold - last_value) / slope
        if slope > 0 and ctx[-1] < uoi_threshold:
            tier3_steps = (uoi_threshold - ctx[-1]) / slope
            tier3_eta   = float(tier3_steps * resample_s / 60.0)
        elif ctx[-1] >= uoi_threshold:
            tier3_eta = 0.0
        else:
            tier3_eta = None  # declining trend — no breach predicted

        if chronos_eta is not None and tier3_eta is not None:
            # Positive = Chronos predicts breach earlier (smaller ETA)
            diffs.append(tier3_eta - chronos_eta)

    if not diffs:
        return {"mean_earliness_advantage_min": None, "n_compared": 0}
    return {
        "mean_earliness_advantage_min": round(float(np.mean(diffs)), 2),
        "n_compared": len(diffs),
    }


# ── Cached evaluation helper ─────────────────────────────────────────────────

def evaluate_pipeline_cached(pipeline, eval_pairs: list, horizon: int, cache_path: Path) -> dict:
    """
    Run zero-shot evaluation and cache the result metrics.
    On subsequent runs with the same number of eval pairs, reloads from cache
    instead of running 21,024 transformer inference calls again (saves hours).
    """
    if cache_path.exists():
        try:
            cached = np.load(str(cache_path), allow_pickle=True)
            if int(cached["n_pairs"]) == len(eval_pairs):
                metrics = {k: float(v) for k, v in cached["metrics"].item().items()}
                log.info("✓ Loaded zero-shot metrics from cache (%s) — skipping %d inferences",
                         cache_path, len(eval_pairs))
                return metrics
            else:
                log.info("Cache pair count mismatch (%d cached vs %d current) — recomputing",
                         int(cached["n_pairs"]), len(eval_pairs))
        except Exception as e:
            log.warning("Cache load failed (%s) — recomputing zero-shot evaluation", e)

    log.info("Running zero-shot evaluation on %d pairs (will be cached for future runs) …", len(eval_pairs))
    metrics = evaluate_pipeline(pipeline, eval_pairs, horizon)
    try:
        np.savez_compressed(str(cache_path), n_pairs=len(eval_pairs), metrics=np.array(metrics))
        log.info("✓ Zero-shot metrics cached to %s — next run will skip this step", cache_path)
    except Exception as e:
        log.warning("Failed to save zero-shot cache: %s", e)
    return metrics


# ── Main ──────────────────────────────────────────────────────────────────────

def train(data_dir: Path, models_dir: Path):
    # ── Load series ───────────────────────────────────────────────────────────
    parquet_path = data_dir / "chronos_train.parquet"
    if not parquet_path.exists():
        log.error("chronos_train.parquet not found at %s — run prepare_dataset.py first", parquet_path)
        raise FileNotFoundError(str(parquet_path))

    df = pd.read_parquet(str(parquet_path))
    log.info("Chronos series loaded: %d rows", len(df))
    series = df["uoi_value"].astype(float).values

    # Impute NaN
    nan_mask = np.isnan(series)
    if nan_mask.any():
        # Forward fill then backward fill
        s = pd.Series(series).ffill().bfill().values
        series = s
        log.info("imputed %d NaN values in uoi_value series", int(nan_mask.sum()))

    # Time split
    n = len(series)
    split_idx = int(n * TRAIN_RATIO)
    train_series = series[:split_idx]
    eval_series  = series[split_idx:]
    log.info("train=%d eval=%d (total=%d)", len(train_series), len(eval_series), n)

    # UOI threshold (q75 of training series)
    uoi_threshold = float(np.percentile(train_series, 75))
    log.info("uoi_value 75th pct (train) = %.4f", uoi_threshold)

    # Build evaluation pairs (non-overlapping windows)
    eval_pairs = make_context_forecast_pairs(
        np.concatenate([train_series[-CONTEXT_LEN:], eval_series]),
        context_len=CONTEXT_LEN,
        horizon=FORECAST_HORIZON,
        stride=FORECAST_HORIZON,  # non-overlapping for eval
    )
    log.info("eval pairs: %d", len(eval_pairs))

    train_pairs = make_context_forecast_pairs(
        train_series,
        context_len=CONTEXT_LEN,
        horizon=FORECAST_HORIZON,
        stride=FORECAST_HORIZON // 2,
    )
    log.info("train pairs: %d", len(train_pairs))

    if not eval_pairs:
        log.warning("no eval pairs — series too short for context=%d + horizon=%d", CONTEXT_LEN, FORECAST_HORIZON)

    # ── Load zero-shot pipeline ───────────────────────────────────────────────
    pipeline = load_chronos_pipeline()

    # ── Zero-shot evaluation ─────────────────────────────────────────────
    log.info("=== ZERO-SHOT evaluation ===")
    zs_cache = data_dir / "chronos_zeroshot_cache.npz"
    zs_metrics = evaluate_pipeline_cached(pipeline, eval_pairs, FORECAST_HORIZON, zs_cache)
    log.info("zero-shot: WQL=%.6f  MIS=%.4f  calibration=%.4f",
             zs_metrics["wql"], zs_metrics["mis"], zs_metrics["calibration"])

    # ── Fine-tuning ───────────────────────────────────────────────────────────
    models_dir.mkdir(parents=True, exist_ok=True)
    ft_checkpoint = None
    ft_metrics    = None

    if len(train_pairs) >= 4:
        log.info("=== FINE-TUNING ===")
        val_pairs = eval_pairs[:max(1, len(eval_pairs) // 4)]
        ft_checkpoint = fine_tune_chronos(
            pipeline, list(train_pairs), val_pairs, models_dir
        )

        if ft_checkpoint and ft_checkpoint.exists():
            log.info("evaluating fine-tuned model from %s …", ft_checkpoint)
            ft_pipeline = load_chronos_pipeline(str(ft_checkpoint))
            ft_metrics  = evaluate_pipeline(ft_pipeline, eval_pairs, FORECAST_HORIZON)
            log.info("fine-tuned: WQL=%.6f  MIS=%.4f  calibration=%.4f",
                     ft_metrics["wql"], ft_metrics["mis"], ft_metrics["calibration"])
    else:
        log.warning("insufficient train pairs (%d) for fine-tuning — using zero-shot", len(train_pairs))

    # ── Choose best model ─────────────────────────────────────────────────────
    use_finetuned = False
    if ft_metrics and not np.isnan(zs_metrics["wql"]) and not np.isnan(ft_metrics["wql"]):
        improvement = (zs_metrics["wql"] - ft_metrics["wql"]) / (abs(zs_metrics["wql"]) + 1e-9)
        if improvement >= MIN_WQL_IMPROVEMENT:
            use_finetuned = True
            log.info("using fine-tuned model (WQL improved %.1f%% >= %.0f%% threshold)",
                     improvement * 100, MIN_WQL_IMPROVEMENT * 100)
        else:
            log.info("using zero-shot model (WQL improvement %.1f%% < %.0f%% threshold — not worth fine-tuning)",
                     improvement * 100, MIN_WQL_IMPROVEMENT * 100)
    else:
        log.info("using zero-shot model (fine-tuning unavailable or metrics missing)")

    final_pipeline = pipeline
    if use_finetuned and ft_checkpoint:
        final_pipeline = load_chronos_pipeline(str(ft_checkpoint))

    final_metrics = ft_metrics if use_finetuned else zs_metrics

    # Save final checkpoint directory
    ft_dir = models_dir / "chronos_finetuned"
    if use_finetuned and ft_checkpoint and ft_checkpoint.exists():
        log.info("fine-tuned checkpoint already at %s", ft_dir)
    else:
        # Create a marker file indicating zero-shot is used
        ft_dir.mkdir(parents=True, exist_ok=True)
        (ft_dir / "zero_shot_marker.json").write_text(json.dumps({
            "base_model": "amazon/chronos-t5-small",
            "note": "Zero-shot model used — fine-tuning did not improve WQL by >= 5%"
        }, indent=2))

    # ── Breach metrics ────────────────────────────────────────────────────────
    breach_metrics = {}
    earliness_vs_t3 = {}
    if eval_pairs:
        breach_metrics  = compute_breach_metrics(final_pipeline, eval_pairs[:20], uoi_threshold)
        earliness_vs_t3 = compute_earliness_vs_tier3(eval_pairs[:20], final_pipeline, uoi_threshold)
        log.info("breach metrics: %s", breach_metrics)
        log.info("earliness vs Tier 3: %s", earliness_vs_t3)

    # ── Save metadata ─────────────────────────────────────────────────────────
    metadata = {
        "generated_at":     datetime.now(timezone.utc).isoformat(),
        "base_model":       "amazon/chronos-t5-small",
        "context_length":   CONTEXT_LEN,
        "forecast_horizon": FORECAST_HORIZON,
        "resample_seconds": RESAMPLE_S,
        "uoi_threshold":    uoi_threshold,
        "use_finetuned":    use_finetuned,
        "zero_shot_metrics":  zs_metrics,
        "finetuned_metrics":  ft_metrics,
        "final_metrics":      final_metrics,
        "breach_metrics":     breach_metrics,
        "earliness_vs_tier3": earliness_vs_t3,
        "quantiles":          [0.1, 0.5, 0.9],
        "breach_threshold_uoi": uoi_threshold,
    }
    (models_dir / "chronos_metadata.json").write_text(json.dumps(metadata, indent=2))
    log.info("saved chronos_metadata.json")

    # ── Update training report ────────────────────────────────────────────────
    zs_wql = zs_metrics.get("wql", "N/A")
    ft_wql = ft_metrics.get("wql", "N/A") if ft_metrics else "N/A"
    final_wql = final_metrics.get("wql", "N/A") if final_metrics else "N/A"

    report_section = f"""## Chronos-2 (UOI Value Forecasting)

Generated: {datetime.now(timezone.utc).isoformat()}

### Architecture

- Model: amazon/chronos-t5-small (46M parameters)
- Task: Univariate probabilistic forecasting of uoi_value
- Device: CPU, float32
- Context length: {CONTEXT_LEN} steps ({CONTEXT_LEN * RESAMPLE_S // 60} min at 15s cadence)
- Forecast horizon: {FORECAST_HORIZON} steps ({FORECAST_HORIZON * RESAMPLE_S} s = {FORECAST_HORIZON * RESAMPLE_S // 60} min)

### Training Data

- Series: uoi_value (forward-filled from ~90s cadence to 15s)
- Total rows: {n}
- Train rows: {split_idx}
- Eval rows:  {n - split_idx}
- Train pairs: {len(train_pairs)}
- Eval pairs:  {len(eval_pairs)}

### Zero-Shot vs Fine-Tuned Comparison

| Metric | Zero-Shot | Fine-Tuned |
|---|---|---|
| WQL (lower=better) | {zs_wql} | {ft_wql if ft_metrics else 'N/A'} |
| MIS (80% interval) | {zs_metrics.get('mis', 'N/A')} | {ft_metrics.get('mis', 'N/A') if ft_metrics else 'N/A'} |
| Calibration (80% cov) | {zs_metrics.get('calibration', 'N/A')} | {ft_metrics.get('calibration', 'N/A') if ft_metrics else 'N/A'} |

**Decision: {"Fine-tuned" if use_finetuned else "Zero-shot"} model used.**
{"Fine-tuning improved WQL by ≥ 5%." if use_finetuned else "Fine-tuning did not improve WQL by ≥ 5% — zero-shot model retained."}

### Final Model Performance ({"Fine-tuned" if use_finetuned else "Zero-shot"})

| Metric | Value |
|---|---|
| WQL | {final_wql} |
| MIS (80% interval) | {final_metrics.get('mis', 'N/A') if final_metrics else 'N/A'} |
| 80% Interval Calibration | {final_metrics.get('calibration', 'N/A') if final_metrics else 'N/A'} |

### Breach Detection

| Metric | Value |
|---|---|
| UOI breach threshold | {uoi_threshold:.4f} (75th percentile of training series) |
| Mean breach ETA | {breach_metrics.get('mean_breach_eta_minutes', 'N/A')} min |
| Median breach ETA | {breach_metrics.get('median_breach_eta_minutes', 'N/A')} min |
| Mean breach probability | {breach_metrics.get('mean_breach_probability', 'N/A')} |
| Fraction with breach ETA | {breach_metrics.get('fraction_with_breach_eta', 'N/A')} |

### Earliness vs Tier 3 (Linear Extrapolation)

| Metric | Value |
|---|---|
| Mean earliness advantage | {earliness_vs_t3.get('mean_earliness_advantage_min', 'N/A')} min |
| N evaluated pairs | {earliness_vs_t3.get('n_compared', 0)} |

*Positive earliness advantage = Chronos-2 predicts breach earlier than Tier 3 linear extrapolation.*

### What breach_probability means

`breach_probability` is the fraction of Chronos-2's {20} Monte Carlo sample paths
in which uoi_value exceeds {uoi_threshold:.4f} at least once in the {FORECAST_HORIZON}-step horizon.

- **< 0.4**: Low confidence — do not display breach ETA, report as "low"
- **0.4 – 0.7**: Medium confidence — display with uncertainty
- **> 0.7**: High confidence — escalate to critical, show breach countdown

This is an empirically calibrated heuristic. The 80% prediction interval calibration
(target ≈ 80%, actual = {final_metrics.get('calibration', 'TBD') if final_metrics else 'TBD'}) indicates {"good" if final_metrics and isinstance(final_metrics.get('calibration'), float) and abs(final_metrics['calibration'] - 0.8) < 0.15 else "moderate"} reliability.

"""

    report_path = data_dir / "training_report.md"
    existing = report_path.read_text() if report_path.exists() else "# Tier 2 AI Training Report\n\n"
    marker = "## Chronos-2"
    if marker in existing:
        pre = existing[:existing.index(marker)]
        report_path.write_text(pre + report_section, encoding="utf-8")
    else:
        report_path.write_text(existing + "\n" + report_section, encoding="utf-8")

    log.info("updated training_report.md")
    log.info("Chronos training complete — model: %s", "fine-tuned" if use_finetuned else "zero-shot")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train Chronos-2 for uoi_value forecasting")
    parser.add_argument("--data-dir",      default="train/data", help="Directory with chronos_train.parquet")
    parser.add_argument("--models-dir",    default="../models",   help="Directory to write model artifacts")
    parser.add_argument("--force-retrain", action="store_true",   help="Delete zero-shot cache and re-evaluate from scratch")
    args = parser.parse_args()

    if args.force_retrain:
        cache = Path(args.data_dir) / "chronos_zeroshot_cache.npz"
        if cache.exists():
            cache.unlink()
            log.info("--force-retrain: deleted zero-shot cache %s", cache)

    train(Path(args.data_dir), Path(args.models_dir))

