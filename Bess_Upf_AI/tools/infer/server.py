"""
Tier 2 ML inference sidecar.

Loads the trained models at startup and serves a single POST /predict endpoint
that scores a feature vector against both models.

POST /predict
    Body: {"features": {"rate_pfcp_sessions": 12.3, ...}}
    Response: InferResponse (see schema below)

GET /health
    Response: {"available": bool, "models_loaded": bool}

The sidecar is internal-network only — never exposed through the reverse proxy.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import joblib
import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

MODELS_DIR = Path(os.getenv("MODELS_DIR", "/models"))
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))

app = FastAPI(title="BESS-UPF ML Inference Sidecar", docs_url=None, redoc_url=None)


# ── Model state (loaded once at startup) ─────────────────────────────────────

class _State:
    available: bool = False
    feature_names: list[str] = []
    scaler = None
    clf_if = None
    clf_rf = None
    feat_importances: list[tuple[str, float]] = []  # sorted descending


_state = _State()


def _load_models():
    required = [
        MODELS_DIR / "isolation_forest.joblib",
        MODELS_DIR / "random_forest.joblib",
        MODELS_DIR / "scaler.joblib",
        MODELS_DIR / "feature_columns.json",
    ]
    missing = [p for p in required if not p.exists()]
    if missing:
        log.warning("models not found (%s) — Tier 2 unavailable until trained",
                    ", ".join(p.name for p in missing))
        return

    try:
        # joblib (pickle-based) is safe here: files are written by our own train.py
        # and loaded only from the models/ Docker volume — never from user input or network.
        _state.clf_if       = joblib.load(str(MODELS_DIR / "isolation_forest.joblib"))
        _state.clf_rf       = joblib.load(str(MODELS_DIR / "random_forest.joblib"))
        _state.scaler       = joblib.load(str(MODELS_DIR / "scaler.joblib"))
        _state.feature_names = json.loads((MODELS_DIR / "feature_columns.json").read_text())

        # Pre-sort RF feature importances for fast top-N lookup
        importances = _state.clf_rf.feature_importances_
        _state.feat_importances = sorted(
            zip(_state.feature_names, importances.tolist()),
            key=lambda x: x[1],
            reverse=True,
        )

        _state.available = True
        log.info("models loaded from %s (%d features)", MODELS_DIR, len(_state.feature_names))
    except Exception as e:
        log.error("failed to load models: %s", e)


@app.on_event("startup")
def startup():
    _load_models()


# ── API schemas ───────────────────────────────────────────────────────────────

class PredictRequest(BaseModel):
    features: dict[str, float]


class FeatureContribution(BaseModel):
    name: str
    importance: float


class InferResponse(BaseModel):
    available: bool
    if_score: float | None = None           # Isolation Forest decision_function value (higher = more normal)
    rf_score: float | None = None           # RF predict_proba anomaly class probability [0, 1]
    rf_class: str | None = None             # "normal" or "anomaly" (binary)
    feature_contributions: list[FeatureContribution] = []


# ── Endpoint ──────────────────────────────────────────────────────────────────

@app.post("/predict", response_model=InferResponse)
def predict(req: PredictRequest):
    if not _state.available:
        return InferResponse(available=False)

    # Build feature vector in the exact order expected by the scaler/models
    vec = np.array(
        [req.features.get(name, float("nan")) for name in _state.feature_names],
        dtype=float,
    )

    # Impute NaN with training mean (stored in scaler.mean_)
    nan_mask = np.isnan(vec)
    if nan_mask.any():
        vec[nan_mask] = _state.scaler.mean_[nan_mask]

    vec_sc = _state.scaler.transform(vec.reshape(1, -1))[0]

    # IF score
    if_score = float(_state.clf_if.decision_function(vec_sc.reshape(1, -1))[0])

    # RF score and top-3 feature contributions
    rf_proba = _state.clf_rf.predict_proba(vec_sc.reshape(1, -1))[0]
    rf_score = float(rf_proba[1])  # probability of anomaly class
    rf_class  = "anomaly" if rf_score >= 0.5 else "normal"

    # Feature contributions: top-3 from RF feature_importances_
    top3 = [
        FeatureContribution(name=name, importance=round(imp, 4))
        for name, imp in _state.feat_importances[:3]
    ]

    return InferResponse(
        available=True,
        if_score=round(if_score, 6),
        rf_score=round(rf_score, 6),
        rf_class=rf_class,
        feature_contributions=top3,
    )


@app.get("/health")
def health():
    return {"available": _state.available, "models_loaded": _state.available}


if __name__ == "__main__":
    uvicorn.run("server:app", host=HOST, port=PORT, app_dir=str(Path(__file__).parent), log_level="info")
