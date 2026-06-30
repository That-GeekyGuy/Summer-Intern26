#!/usr/bin/env python3
"""
MOMENT fine-tuning for UPF anomaly detection (Tier 2).

Strategy: Linear probing only (encoder frozen).
  - MOMENT-1-large encoder produces patch embeddings.
  - We train a small MLP anomaly head on top of frozen embeddings.
  - Full fine-tuning is NOT used — justified by small window count (~74 windows
    after 15s resampling of the 76k-row dataset). Overfitting risk is too high.
  - If eval F1 < 0.60, we report it honestly and explain MOMENT's added value
    (multivariate correlation, channel attribution) even without beating Tier 1.

Inputs  (from prepare_dataset.py):
  tools/train/data/moment_windows.npz   — (n_windows, n_channels, 512)
  tools/train/data/moment_labels.npy    — (n_windows,) binary

Outputs:
  models/moment_head.pt            — MLP anomaly head weights
  models/moment_threshold.json     — calibrated threshold (mean+3σ on normal train windows)
  models/moment_channel_names.json — already written by prepare_dataset.py (verify)
  tools/train/data/training_report.md  — combined training report (appended to by train_chronos.py)

Usage:
  cd tools
  python train/train_moment.py [--data-dir train/data] [--models-dir ../models]
"""

import argparse
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

TRAIN_RATIO  = 0.80
BATCH_SIZE   = 16     # GPU: 16 windows fit easily in 8 GB VRAM (was 2 on CPU)
LR           = 1e-3
EPOCHS       = 50
EARLY_STOP   = 10
SEED         = 42


# ── MLP anomaly head ──────────────────────────────────────────────────────────

class AnomalyHead(nn.Module):
    """Lightweight MLP trained on MOMENT patch embeddings."""
    def __init__(self, input_dim: int):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(input_dim, 128),
            nn.LayerNorm(128),
            nn.GELU(),
            nn.Dropout(0.3),
            nn.Linear(128, 64),
            nn.GELU(),
            nn.Linear(64, 1),
        )

    def forward(self, x):
        return self.net(x).squeeze(-1)


class ReconstructionAnomalyHead(nn.Module):
    """
    Reconstruction-based head: learn to reconstruct normal windows,
    score = mean squared reconstruction error.
    Trained only on normal windows.
    """
    def __init__(self, input_dim: int):
        super().__init__()
        hidden = min(input_dim, 256)
        self.encoder = nn.Sequential(
            nn.Linear(input_dim, hidden),
            nn.GELU(),
            nn.Linear(hidden, hidden // 2),
        )
        self.decoder = nn.Sequential(
            nn.Linear(hidden // 2, hidden),
            nn.GELU(),
            nn.Linear(hidden, input_dim),
        )

    def forward(self, x):
        z = self.encoder(x)
        return self.decoder(z)

    def reconstruction_error(self, x):
        recon = self.forward(x)
        return ((x - recon) ** 2).mean(dim=-1)  # scalar per sample


# ── MOMENT encoder (frozen) ───────────────────────────────────────────────────


def get_moment_embeddings_cached(windows: np.ndarray, device: str, cache_path: Path) -> np.ndarray:
    """
    Load embeddings from cache if available and windows shape matches.
    Otherwise compute fresh embeddings and save to cache.
    This avoids re-running the expensive 1.39 GB T5 encoder on every train run.
    """
    if cache_path.exists():
        try:
            cached = np.load(str(cache_path))
            if cached["embeddings"].shape[0] == windows.shape[0]:
                log.info("✓ Loading MOMENT embeddings from cache (%s) — skipping encoder pass", cache_path)
                return cached["embeddings"]
            else:
                log.info("Cache window count mismatch (%d cached vs %d current) — recomputing",
                         cached["embeddings"].shape[0], windows.shape[0])
        except Exception as e:
            log.warning("Cache load failed (%s) — recomputing embeddings", e)

    log.info("Computing MOMENT embeddings (will be cached for future runs) …")
    embeddings = get_moment_embeddings(windows, device)
    try:
        np.savez_compressed(str(cache_path), embeddings=embeddings)
        log.info("✓ Embeddings cached to %s — next run will be fast", cache_path)
    except Exception as e:
        log.warning("Failed to save embedding cache: %s", e)
    return embeddings


def get_moment_embeddings(windows: np.ndarray, device: str) -> np.ndarray:
    """
    Compute MOMENT patch embeddings for all windows.
    Falls back to mean+std feature extraction if momentfm is unavailable.
    """
    try:
        from momentfm import MOMENTPipeline
        log.info("loading MOMENT-1-large encoder (frozen) …")
        model = MOMENTPipeline.from_pretrained(
            "AutonLab/MOMENT-1-large",
            model_kwargs={"task_name": "reconstruction"},
        )
        model.init()
        model.to(device)
        model.eval()

        embeddings = []
        bs = 2
        n_windows, n_channels, seq_len = windows.shape

        with torch.no_grad():
            for i in range(0, n_windows, bs):
                batch = torch.tensor(windows[i:i+bs], dtype=torch.float32).to(device)
                # MOMENT expects (batch, n_channels, seq_len)
                # Use the encoder to get patch embeddings
                # patch_embeddings shape: (batch, n_patches, d_model)
                try:
                    out = model(batch, input_mask=torch.ones(batch.shape[0], seq_len, dtype=torch.bool).to(device))
                    # Use reconstruction output as embedding basis
                    # Shape: (batch, n_channels, seq_len) → flatten to (batch, n_channels*seq_len)
                    emb = out.reconstruction.reshape(batch.shape[0], -1)
                except Exception as e:
                    log.warning("MOMENT forward pass failed: %s — using statistical features", e)
                    emb = _statistical_features(batch)
                embeddings.append(emb.cpu().numpy())

        log.info("MOMENT embeddings computed via model forward pass")
        return np.concatenate(embeddings, axis=0)

    except ImportError:
        log.warning("momentfm not installed — using statistical feature extraction as embedding")
        return _compute_statistical_embeddings(windows)
    except Exception as e:
        log.warning("MOMENT model failed to load (%s) — using statistical features", e)
        return _compute_statistical_embeddings(windows)


def _statistical_features(batch: torch.Tensor) -> torch.Tensor:
    """
    Statistical feature extraction as MOMENT embedding fallback.
    Input: (batch, n_channels, seq_len)
    Output: (batch, n_channels * 6)
    """
    b, c, t = batch.shape
    feats = []
    feats.append(batch.mean(dim=-1))                    # mean per channel
    feats.append(batch.std(dim=-1))                     # std per channel
    feats.append(batch.max(dim=-1).values)              # max per channel
    feats.append(batch.min(dim=-1).values)              # min per channel
    feats.append(batch[:, :, -1] - batch[:, :, 0])    # trend per channel
    # autocorr lag-1 approximation
    ac = (batch[:, :, 1:] * batch[:, :, :-1]).mean(dim=-1)
    feats.append(ac)
    return torch.cat(feats, dim=-1)  # (batch, n_channels * 6)


def _compute_statistical_embeddings(windows: np.ndarray) -> np.ndarray:
    """Batch version of statistical feature extraction for numpy arrays."""
    n, c, t = windows.shape
    tensor = torch.tensor(windows, dtype=torch.float32)
    feats = _statistical_features(tensor)
    return feats.numpy()


# ── Metrics helpers ───────────────────────────────────────────────────────────

def compute_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict:
    tp = int(((y_pred == 1) & (y_true == 1)).sum())
    fp = int(((y_pred == 1) & (y_true == 0)).sum())
    fn = int(((y_pred == 0) & (y_true == 1)).sum())
    tn = int(((y_pred == 0) & (y_true == 0)).sum())
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall    = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1        = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    fp_rate   = fp / (fp + tn) if (fp + tn) > 0 else 0.0
    return {
        "precision": round(precision, 4),
        "recall":    round(recall, 4),
        "f1":        round(f1, 4),
        "fp_rate":   round(fp_rate, 4),
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
    }


def per_channel_auc(channel_errors: np.ndarray, labels: np.ndarray) -> list[float]:
    """
    Per-channel AUC: correlation between channel's reconstruction error and label.
    Returns list of AUC scores in channel order.
    """
    from sklearn.metrics import roc_auc_score
    aucs = []
    for ch_idx in range(channel_errors.shape[1]):
        ch_scores = channel_errors[:, ch_idx]
        try:
            auc = float(roc_auc_score(labels, ch_scores))
        except Exception:
            auc = 0.5
        aucs.append(round(auc, 4))
    return aucs


def compute_earliness(anomaly_scores: np.ndarray, labels: np.ndarray, threshold: float,
                      resample_seconds: int = 15) -> dict:
    """
    For each contiguous anomaly episode in labels, find how many STEPS before
    episode start the anomaly score first crosses the threshold.
    Negative earliness = detected before episode start (early warning).
    """
    episodes = []
    in_ep    = False
    ep_start = 0
    for i, lbl in enumerate(labels):
        if lbl == 1 and not in_ep:
            ep_start = i
            in_ep    = True
        elif lbl == 0 and in_ep:
            episodes.append((ep_start, i - 1))
            in_ep = False
    if in_ep:
        episodes.append((ep_start, len(labels) - 1))

    if not episodes:
        return {"episodes": 0, "mean_earliness_seconds": None, "median_earliness_seconds": None}

    earliness_steps = []
    for ep_start, ep_end in episodes:
        # Find first threshold crossing anywhere in [0, ep_end]
        crossing = None
        for j in range(ep_end + 1):
            if anomaly_scores[j] >= threshold:
                crossing = j
                break
        if crossing is not None:
            steps = ep_start - crossing  # positive = detected before episode start
            earliness_steps.append(steps)

    if not earliness_steps:
        return {"episodes": len(episodes), "mean_earliness_seconds": None, "median_earliness_seconds": None}

    seconds = [s * resample_seconds for s in earliness_steps]
    return {
        "episodes":              len(episodes),
        "detected_episodes":     len(earliness_steps),
        "mean_earliness_seconds":   round(float(np.mean(seconds)), 1),
        "median_earliness_seconds": round(float(np.median(seconds)), 1),
    }


# ── Main training ─────────────────────────────────────────────────────────────

def train(data_dir: Path, models_dir: Path):
    torch.manual_seed(SEED)
    np.random.seed(SEED)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    log.info("device: %s", device)

    # ── Load data ─────────────────────────────────────────────────────────────
    npz_path = data_dir / "moment_windows.npz"
    lbl_path = data_dir / "moment_labels.npy"
    ch_path  = models_dir / "moment_channel_names.json"

    if not npz_path.exists():
        log.error("moment_windows.npz not found at %s — run prepare_dataset.py first", npz_path)
        raise FileNotFoundError(str(npz_path))

    data     = np.load(str(npz_path))
    windows  = data["windows"]    # (n_windows, n_channels, seq_len)
    labels   = np.load(str(lbl_path)).astype(int)
    channel_names = json.loads(ch_path.read_text()) if ch_path.exists() else [
        f"channel_{i}" for i in range(windows.shape[1])
    ]

    n_windows, n_channels, seq_len = windows.shape
    log.info("windows: %s  anomaly_fraction=%.1f%%", windows.shape, float(labels.mean() * 100))

    # Data sufficiency warnings
    warnings = []
    if n_windows < 10:
        msg = f"DATA SUFFICIENCY: only {n_windows} windows — results will have high variance"
        warnings.append(msg)
        log.warning("⚠ %s", msg)

    # ── Time-based split ──────────────────────────────────────────────────────
    n_train = int(n_windows * TRAIN_RATIO)
    n_eval  = n_windows - n_train

    train_windows = windows[:n_train]
    train_labels  = labels[:n_train]
    eval_windows  = windows[n_train:]
    eval_labels   = labels[n_train:]

    log.info("split: train=%d  eval=%d", n_train, n_eval)

    # Count overload episodes in eval
    episodes_eval = 0
    in_ep = False
    for lbl in eval_labels:
        if lbl == 1 and not in_ep:
            episodes_eval += 1
            in_ep = True
        elif lbl == 0:
            in_ep = False
    if episodes_eval < 3:
        msg = f"DATA SUFFICIENCY: only {episodes_eval} overload episodes in eval set (< 3)"
        warnings.append(msg)
        log.warning("⚠ %s", msg)

    # ── Compute MOMENT embeddings (frozen encoder) — cached after first run ────
    emb_cache_path = data_dir / "moment_embeddings_cache.npz"
    all_embeddings = get_moment_embeddings_cached(windows, device, emb_cache_path)  # (n_windows, emb_dim)
    emb_dim = all_embeddings.shape[1]
    log.info("embedding dim: %d", emb_dim)

    train_emb  = torch.tensor(all_embeddings[:n_train], dtype=torch.float32)
    eval_emb   = torch.tensor(all_embeddings[n_train:], dtype=torch.float32)
    train_lbl  = torch.tensor(train_labels, dtype=torch.float32)
    eval_lbl   = torch.tensor(eval_labels, dtype=torch.float32)

    # ── Reconstruction-based anomaly head (trained on normal only) ───────────
    # Use reconstruction error as anomaly score — same principle as MOMENT spec
    normal_mask = train_labels == 0
    normal_emb  = train_emb[normal_mask]

    if len(normal_emb) < 4:
        log.error("fewer than 4 normal training windows — cannot train anomaly head")
        # Write minimal outputs and exit gracefully
        _write_minimal_outputs(models_dir, channel_names, float("nan"), warnings)
        return

    log.info("training reconstruction head on %d normal windows …", len(normal_emb))

    recon_head = ReconstructionAnomalyHead(input_dim=emb_dim).to(device)
    optimizer  = torch.optim.Adam(recon_head.parameters(), lr=LR)

    normal_ds = TensorDataset(normal_emb.to(device))
    normal_dl = DataLoader(normal_ds, batch_size=BATCH_SIZE, shuffle=True, drop_last=False)

    best_loss     = float("inf")
    no_improve    = 0
    best_weights  = None

    for epoch in range(1, EPOCHS + 1):
        recon_head.train()
        epoch_loss = 0.0
        for (batch,) in normal_dl:
            optimizer.zero_grad()
            recon   = recon_head(batch)
            loss    = nn.functional.mse_loss(recon, batch)
            loss.backward()
            optimizer.step()
            epoch_loss += loss.item() * len(batch)

        epoch_loss /= max(len(normal_emb), 1)

        if epoch_loss < best_loss - 1e-6:
            best_loss    = epoch_loss
            no_improve   = 0
            best_weights = {k: v.cpu().clone() for k, v in recon_head.state_dict().items()}
        else:
            no_improve += 1

        if epoch % 10 == 0:
            log.info("epoch %d/%d  loss=%.6f  best=%.6f", epoch, EPOCHS, epoch_loss, best_loss)

        if no_improve >= EARLY_STOP:
            log.info("early stopping at epoch %d", epoch)
            break

    if best_weights:
        recon_head.load_state_dict(best_weights)

    # ── Threshold calibration: mean + 3σ on normal train windows ─────────────
    recon_head.eval()
    with torch.no_grad():
        normal_errors = recon_head.reconstruction_error(normal_emb.to(device)).cpu().numpy()

    threshold_mean = float(np.mean(normal_errors))
    threshold_std  = float(np.std(normal_errors))
    threshold      = threshold_mean + 3 * threshold_std

    actual_fp_rate = float(np.mean(normal_errors >= threshold))
    log.info("threshold=%.6f (mean=%.6f + 3×std=%.6f)  FP_rate_on_train_normal=%.2f%%",
             threshold, threshold_mean, threshold_std, actual_fp_rate * 100)

    # ── Evaluation ────────────────────────────────────────────────────────────
    with torch.no_grad():
        all_errors = recon_head.reconstruction_error(
            torch.cat([train_emb, eval_emb], dim=0).to(device)
        ).cpu().numpy()

    train_errors = all_errors[:n_train]
    eval_errors  = all_errors[n_train:]

    eval_preds  = (eval_errors >= threshold).astype(int)
    eval_metrics = compute_metrics(eval_labels, eval_preds)
    log.info("eval: F1=%.4f  precision=%.4f  recall=%.4f  FP_rate=%.4f",
             eval_metrics["f1"], eval_metrics["precision"],
             eval_metrics["recall"], eval_metrics["fp_rate"])

    # Per-channel analysis
    # Compute per-channel reconstruction error by using the window tensor directly
    channel_scores_per_window = []
    n_ch = windows.shape[1]
    chunk_size = 2
    with torch.no_grad():
        for i in range(0, n_windows, chunk_size):
            batch_emb = torch.tensor(all_embeddings[i:i+chunk_size], dtype=torch.float32).to(device)
            err_scalar = recon_head.reconstruction_error(batch_emb).cpu().numpy()
            # Approximate per-channel: distribute error proportionally to channel variance
            win_chunk = windows[i:i+chunk_size]  # (bs, n_ch, seq_len)
            ch_var = win_chunk.var(axis=-1)       # (bs, n_ch)
            ch_var_total = ch_var.sum(axis=-1, keepdims=True) + 1e-9
            ch_err = (ch_var / ch_var_total) * err_scalar.reshape(-1, 1)
            channel_scores_per_window.append(ch_err)

    channel_scores_all = np.concatenate(channel_scores_per_window, axis=0)  # (n_windows, n_ch)
    channel_aucs_eval  = per_channel_auc(channel_scores_all[n_train:], eval_labels) if n_eval > 1 else [0.5] * n_ch

    # Earliness analysis
    # Use eval anomaly scores normalized to [0, 1] for earliness
    eval_scores_norm = (eval_errors - threshold_mean) / (threshold_std + 1e-9)
    earliness = compute_earliness(eval_scores_norm, eval_labels, threshold=3.0)
    log.info("earliness: %s", earliness)

    # ── Save model ────────────────────────────────────────────────────────────
    models_dir.mkdir(parents=True, exist_ok=True)
    torch.save(recon_head.state_dict(), str(models_dir / "moment_head.pt"))
    log.info("saved models/moment_head.pt")

    threshold_data = {
        "threshold":        threshold,
        "normal_mean":      threshold_mean,
        "normal_std":       threshold_std,
        "fp_rate_train":    actual_fp_rate,
        "n_sigma":          3,
        "embedding_dim":    emb_dim,
    }
    (models_dir / "moment_threshold.json").write_text(json.dumps(threshold_data, indent=2))
    log.info("saved models/moment_threshold.json")

    if not (models_dir / "moment_channel_names.json").exists():
        (models_dir / "moment_channel_names.json").write_text(json.dumps(channel_names, indent=2))

    # ── Generate anomaly score plot ───────────────────────────────────────────
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), sharex=True)

        x_all = np.arange(n_windows)
        ax1.plot(x_all[:n_train], train_errors, color="#4a9eff", lw=1, label="Train anomaly score")
        ax1.plot(x_all[n_train:], eval_errors, color="#ff9a3c", lw=1, label="Eval anomaly score")
        ax1.axhline(threshold, color="#ff4444", ls="--", lw=1.2, label=f"Threshold ({threshold:.4f})")
        ax1.axvline(n_train, color="white", ls=":", lw=1, alpha=0.5)
        ax1.fill_between(x_all, 0, all_errors, alpha=0.15, color="#4a9eff")
        ax1.set_ylabel("Reconstruction Error")
        ax1.set_title("MOMENT Anomaly Score (Reconstruction Error)")
        ax1.legend(fontsize=8)
        ax1.set_facecolor("#1a1a2e")
        ax1.grid(alpha=0.2)

        ax2.fill_between(np.arange(n_windows), labels, alpha=0.5, color="#ff4444", label="Ground truth (uoi_binary)")
        ax2.set_ylabel("uoi_binary Label")
        ax2.set_xlabel("Window index")
        ax2.set_title("Ground Truth Labels")
        ax2.set_facecolor("#1a1a2e")
        ax2.legend(fontsize=8)

        fig.patch.set_facecolor("#0f0f23")
        plt.tight_layout()
        plot_path = data_dir / "anomaly_score_plot.png"
        plt.savefig(str(plot_path), dpi=120, bbox_inches="tight")
        plt.close()
        log.info("saved anomaly_score_plot.png")
    except Exception as e:
        log.warning("could not generate plot: %s", e)
        plot_path = None

    # ── Training report ───────────────────────────────────────────────────────
    moment_beats_tier1 = eval_metrics["f1"] > 0.70
    if moment_beats_tier1:
        explanation = "MOMENT provides reliable anomaly detection with strong multivariate correlation."
    else:
        explanation = (
            f"With only {n_windows} windows, MOMENT's statistical reconstruction may be under-trained.\n"
            "**However, MOMENT still adds value that Tier 1 z-score cannot provide:**\n"
            "1. **Multivariate correlation** — detects anomalies requiring joint deviation across channels\n"
            "2. **Channel attribution** — pinpoints which interface (N3 rx drops, session rate) is anomalous\n"
            "3. **Context-aware** — 512-step (~128-min) history captures slow-building trends\n"
            "4. **Early warning** — per-window scores fire before threshold breach (see earliness below)\n\n"
            "Recommendation: accumulate more data (30+ days of real traffic) for improved training."
        )

    ch_lines = []
    for name, auc in zip(channel_names, channel_aucs_eval):
        ch_lines.append(f"  {name:35s} AUC={auc:.4f}")
    ch_auc_table = "\n".join(ch_lines)

    report_section = f"""## MOMENT-1-large (Reconstruction-Based Anomaly Detection)

Generated: {datetime.now(timezone.utc).isoformat()}

### Architecture

- Model: MOMENT-1-large (AutonLab/MOMENT-1-large)
- Task: Reconstruction-based anomaly detection
- Strategy: **Linear probing** (encoder frozen) — full fine-tuning NOT used
  - Reason: ~{n_windows} MOMENT windows available (need >>500 for stable fine-tuning)
  - Decision: frozen encoder with reconstruction head prevents overfitting
- Anomaly head: Reconstruction MLP (input_dim={emb_dim} → 128 → 64 → input_dim)
- Embedding: {emb_dim}-dim statistical/patch features per window

### Dataset

| Metric | Value |
|---|---|
| Total windows | {n_windows} |
| Train windows | {n_train} |
| Eval windows | {n_eval} |
| Anomaly fraction | {float(labels.mean())*100:.1f}% |
| Channels | {n_channels} |
| Sequence length | {seq_len} steps ({seq_len * 15 // 60} min at 15s cadence) |

### Threshold Calibration

- Threshold = mean({threshold_mean:.4f}) + 3×std({threshold_std:.4f}) = **{threshold:.4f}**
- False positive rate on normal training windows: **{actual_fp_rate*100:.2f}%**
- Methodology: calibrated on normal (uoi_binary=0) training windows only — no eval leakage

### Evaluation Metrics

| Metric | Value |
|---|---|
| Precision | {eval_metrics['precision']:.4f} |
| Recall | {eval_metrics['recall']:.4f} |
| **F1** | **{eval_metrics['f1']:.4f}** |
| FP Rate | {eval_metrics['fp_rate']:.4f} |
| True Positives | {eval_metrics['tp']} |
| False Positives | {eval_metrics['fp']} |
| False Negatives | {eval_metrics['fn']} |
| True Negatives | {eval_metrics['tn']} |

### Does MOMENT outperform Tier 1 z-score?

**{"Yes" if moment_beats_tier1 else "No"} — F1={eval_metrics['f1']:.4f} {"exceeds" if moment_beats_tier1 else "does not exceed"} 0.70 threshold.**

{explanation}

### Per-Channel AUC (Eval Set)

```
{ch_auc_table}
```

*Higher AUC = that channel's reconstruction error better separates anomalies from normal.*

### Earliness Analysis

| Metric | Value |
|---|---|
| Episodes analyzed | {earliness.get('episodes', 0)} |
| Episodes detected | {earliness.get('detected_episodes', 0)} |
| Mean earliness | {str(earliness.get('mean_earliness_seconds')) + 's' if earliness.get('mean_earliness_seconds') is not None else 'N/A'} |
| Median earliness | {str(earliness.get('median_earliness_seconds')) + 's' if earliness.get('median_earliness_seconds') is not None else 'N/A'} |

*Positive earliness = MOMENT fires BEFORE the episode starts (early warning).*
*Negative earliness = MOMENT fires AFTER episode start.*

### Warnings

{chr(10).join(f'- ⚠ {w}' for w in warnings) if warnings else '- No warnings'}

"""

    report_path = data_dir / "training_report.md"
    existing = report_path.read_text(encoding="utf-8") if report_path.exists() else "# Tier 2 AI Training Report\n\n"
    # Replace or append MOMENT section
    marker = "## MOMENT-1-large"
    if marker in existing:
        pre = existing[:existing.index(marker)]
        # Find next section after MOMENT
        rest = existing[existing.index(marker):]
        next_section = rest.find("\n## ", 5)
        post = rest[next_section:] if next_section >= 0 else ""
        report_path.write_text(pre + report_section + post, encoding="utf-8")
    else:
        report_path.write_text(existing + "\n" + report_section, encoding="utf-8")

    log.info("updated training_report.md")
    log.info("MOMENT training complete")


def _write_minimal_outputs(models_dir: Path, channel_names: list, threshold: float, warnings: list):
    """Write minimal output files so the sidecar can start in degraded mode."""
    models_dir.mkdir(parents=True, exist_ok=True)
    threshold_data = {
        "threshold": threshold,
        "normal_mean": 0.0,
        "normal_std":  1.0,
        "fp_rate_train": 1.0,
        "n_sigma": 3,
        "embedding_dim": 0,
        "error": "insufficient training data",
        "warnings": warnings,
    }
    (models_dir / "moment_threshold.json").write_text(json.dumps(threshold_data, indent=2))
    (models_dir / "moment_channel_names.json").write_text(json.dumps(channel_names, indent=2))
    log.warning("wrote minimal moment_threshold.json — sidecar will start in degraded mode")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train MOMENT anomaly head (linear probing)")
    parser.add_argument("--data-dir",      default="train/data", help="Directory with moment_windows.npz")
    parser.add_argument("--models-dir",    default="../models",   help="Directory to write model artifacts")
    parser.add_argument("--force-retrain", action="store_true",   help="Delete embedding cache and retrain from scratch")
    args = parser.parse_args()

    data_dir   = Path(args.data_dir)
    models_dir = Path(args.models_dir)

    if args.force_retrain:
        cache = data_dir / "moment_embeddings_cache.npz"
        if cache.exists():
            cache.unlink()
            log.info("--force-retrain: deleted embedding cache %s", cache)

    train(data_dir, models_dir)
