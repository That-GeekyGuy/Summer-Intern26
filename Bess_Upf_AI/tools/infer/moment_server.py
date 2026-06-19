"""
MOMENT anomaly detection sidecar.

Internal-network only — never exposed through Caddy.
Loads the frozen MOMENT encoder + trained anomaly head at startup.
Starts in degraded mode (available: false) on any load failure — never crashes.

Endpoints:
  POST /detect  — score a multivariate window
  GET  /health  — model load status, threshold, channel count

Environment:
  MODEL_VARIANT   MOMENT-1-large | MOMENT-1-base (default: MOMENT-1-large)
  MODEL_PATH      path to moment_head.pt              (default: /models/moment_head.pt)
  THRESHOLD_PATH  path to moment_threshold.json        (default: /models/moment_threshold.json)
  CHANNEL_PATH    path to moment_channel_names.json    (default: /models/moment_channel_names.json)
  HOST            bind address                          (default: 0.0.0.0)
  PORT            bind port                             (default: 8083)
"""

import json
import logging
import math
import os
from pathlib import Path
from typing import Any

import numpy as np
import torch
import torch.nn as nn
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

MODELS_DIR     = Path(os.getenv("MODELS_DIR",     "/models"))
MODEL_PATH     = Path(os.getenv("MODEL_PATH",     str(MODELS_DIR / "moment_head.pt")))
THRESHOLD_PATH = Path(os.getenv("THRESHOLD_PATH", str(MODELS_DIR / "moment_threshold.json")))
CHANNEL_PATH   = Path(os.getenv("CHANNEL_PATH",   str(MODELS_DIR / "moment_channel_names.json")))
HOST           = os.getenv("HOST", "0.0.0.0")
PORT           = int(os.getenv("PORT", "8083"))
MODEL_VARIANT  = os.getenv("MODEL_VARIANT", "MOMENT-1-large")

app = FastAPI(title="MOMENT Anomaly Detection Sidecar", docs_url=None, redoc_url=None)


# ── MLP head (must match train_moment.py) ────────────────────────────────────

class ReconstructionAnomalyHead(nn.Module):
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
        return ((x - recon) ** 2).mean(dim=-1)


# ── State ─────────────────────────────────────────────────────────────────────

class _State:
    available: bool   = False
    error_msg: str    = ""
    channel_names: list[str] = []
    threshold: float  = 1.0
    normal_mean: float = 0.0
    normal_std: float  = 1.0
    emb_dim: int       = 0
    head: nn.Module | None = None
    moment_model: Any | None = None
    model_version: str = ""


_state = _State()


def _statistical_features_np(window: np.ndarray) -> np.ndarray:
    """
    Statistical feature extraction as MOMENT embedding fallback.
    Input: (n_channels, seq_len)
    Output: (n_channels * 6,)
    """
    c, t = window.shape
    feats = []
    feats.append(window.mean(axis=-1))
    feats.append(window.std(axis=-1))
    feats.append(window.max(axis=-1))
    feats.append(window.min(axis=-1))
    feats.append(window[:, -1] - window[:, 0])
    ac = (window[:, 1:] * window[:, :-1]).mean(axis=-1)
    feats.append(ac)
    return np.concatenate(feats, axis=0)


def _embed_window(window: np.ndarray) -> np.ndarray:
    """Compute embedding for a single window (n_channels, seq_len)."""
    if _state.moment_model is not None:
        try:
            tensor = torch.tensor(window, dtype=torch.float32).unsqueeze(0)  # (1, c, t)
            _, c, t = tensor.shape
            with torch.no_grad():
                mask = torch.ones(1, t, dtype=torch.bool)
                out  = _state.moment_model(tensor, input_mask=mask)
                emb  = out.reconstruction.reshape(1, -1)
                return emb[0].numpy()
        except Exception as e:
            log.debug("MOMENT model embed failed: %s — using statistical features", e)

    return _statistical_features_np(window)


def _load_models():
    try:
        # Load threshold + channel names
        if not THRESHOLD_PATH.exists():
            _state.error_msg = f"threshold file not found: {THRESHOLD_PATH}"
            log.warning("MOMENT sidecar: %s — starting in degraded mode", _state.error_msg)
            return
        if not CHANNEL_PATH.exists():
            _state.error_msg = f"channel names file not found: {CHANNEL_PATH}"
            log.warning("MOMENT sidecar: %s — starting in degraded mode", _state.error_msg)
            return

        thresh_data = json.loads(THRESHOLD_PATH.read_text())
        _state.threshold    = float(thresh_data.get("threshold", 1.0))
        _state.normal_mean  = float(thresh_data.get("normal_mean", 0.0))
        _state.normal_std   = float(thresh_data.get("normal_std", 1.0))
        _state.emb_dim      = int(thresh_data.get("embedding_dim", 0))
        _state.channel_names = json.loads(CHANNEL_PATH.read_text())

        if _state.emb_dim <= 0:
            _state.error_msg = "embedding_dim=0 in threshold file — model not trained"
            log.warning("MOMENT sidecar: %s", _state.error_msg)
            # Still start degraded — sidecar is functional but cannot score
            return

        # Load head weights
        if MODEL_PATH.exists():
            head = ReconstructionAnomalyHead(input_dim=_state.emb_dim)
            state_dict = torch.load(str(MODEL_PATH), map_location="cpu", weights_only=True)
            head.load_state_dict(state_dict)
            head.eval()
            _state.head = head
            log.info("MOMENT anomaly head loaded (emb_dim=%d)", _state.emb_dim)
        else:
            log.warning("MOMENT head weights not found at %s — using threshold-only mode", MODEL_PATH)

        # Try loading MOMENT encoder (optional — degrades gracefully)
        try:
            from momentfm import MOMENTPipeline
            _state.moment_model = MOMENTPipeline.from_pretrained(
                "AutonLab/MOMENT-1-large",
                model_kwargs={"task_name": "reconstruction"},
            )
            _state.moment_model.init()
            _state.moment_model.eval()
            log.info("MOMENT-1-large encoder loaded")
        except Exception as e:
            log.warning("MOMENT encoder not available (%s) — using statistical features", e)

        _state.model_version = f"{MODEL_VARIANT}@{MODEL_PATH.stat().st_mtime:.0f}" if MODEL_PATH.exists() else "statistical"
        _state.available = True
        log.info("MOMENT sidecar ready — %d channels, threshold=%.6f", len(_state.channel_names), _state.threshold)

    except Exception as e:
        _state.error_msg = str(e)
        log.error("MOMENT sidecar load failed: %s", e)


@app.on_event("startup")
def startup():
    _load_models()


# ── Request / response schemas ────────────────────────────────────────────────

class DetectRequest(BaseModel):
    channels: list[list[float]]       # n_channels × 512 values (ordered)
    channel_names: list[str] | None = None  # optional override of registered names


class ChannelScore(BaseModel):
    channel: str
    score: float


class DetectResponse(BaseModel):
    available: bool
    anomaly: bool
    anomaly_score: float | None = None
    threshold: float | None = None
    channel_scores: dict[str, float] | None = None
    top_anomalous_channels: list[str] | None = None
    confidence: float | None = None
    model_version: str = ""


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/detect", response_model=DetectResponse)
def detect(req: DetectRequest):
    if not _state.available:
        return DetectResponse(available=False, anomaly=False, model_version="degraded")

    # Validate input dimensions
    n_channels = len(req.channels)
    if n_channels == 0:
        return DetectResponse(available=True, anomaly=False, anomaly_score=0.0,
                              threshold=_state.threshold, channel_scores={},
                              top_anomalous_channels=[], confidence=0.0)

    seq_len = len(req.channels[0])
    window  = np.array(req.channels, dtype=np.float32)  # (n_channels, seq_len)

    # Standardize per channel (z-score, same as training)
    for ch_idx in range(n_channels):
        std = float(window[ch_idx].std())
        if std > 1e-9:
            window[ch_idx] = (window[ch_idx] - window[ch_idx].mean()) / std

    # Compute embedding
    emb = _embed_window(window)  # (emb_dim,)

    # Compute anomaly score via reconstruction error
    if _state.head is not None:
        with torch.no_grad():
            t = torch.tensor(emb, dtype=torch.float32).unsqueeze(0)
            score = float(_state.head.reconstruction_error(t).item())
    else:
        # Fallback: z-score of embedding from normal distribution
        norm_score = (emb - _state.normal_mean) / (max(_state.normal_std, 1e-9))
        score = float(np.abs(norm_score).mean())

    anomaly  = score >= _state.threshold
    confidence = _clamp(1.0 - math.exp(-max(0.0, score - _state.threshold) / max(_state.normal_std, 1e-9)), 0.0, 1.0)

    # Per-channel scores (approximate: channel variance contribution × total score)
    ch_names = req.channel_names or _state.channel_names
    ch_var   = window.var(axis=-1)  # (n_channels,)
    total_var = float(ch_var.sum()) + 1e-9
    ch_scores = {
        name: round(float((ch_var[i] / total_var) * score), 6)
        for i, name in enumerate(ch_names[:n_channels])
    }

    top_threshold = _state.threshold * 2.0
    top_channels  = [name for name, s in sorted(ch_scores.items(), key=lambda x: -x[1])
                     if s >= top_threshold]

    return DetectResponse(
        available=True,
        anomaly=anomaly,
        anomaly_score=round(score, 6),
        threshold=round(_state.threshold, 6),
        channel_scores=ch_scores,
        top_anomalous_channels=top_channels[:5],
        confidence=round(confidence, 4),
        model_version=_state.model_version,
    )


@app.get("/health")
def health():
    return {
        "available":     _state.available,
        "model_version": _state.model_version,
        "n_channels":    len(_state.channel_names),
        "threshold":     _state.threshold,
        "error":         _state.error_msg if not _state.available else None,
    }


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


if __name__ == "__main__":
    uvicorn.run("moment_server:app", host=HOST, port=PORT, log_level="info")
