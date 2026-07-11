"""
Chronos-2 forecast sidecar.

Serves POST /intervals for analysis/internal/api/handler.go's handleIntervals,
which sends {channel, context, horizon} and expects back p10/p50/p90 uncertainty
bands. Loads amazon/chronos-t5-small zero-shot (or a fine-tuned checkpoint under
MODELS_DIR/chronos_finetuned, if chronos_metadata.json says use_finetuned=true) —
same model selection and prediction logic as tools/train/train_chronos.py's
load_chronos_pipeline/predict_chronos, kept in sync deliberately.
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import torch
from fastapi import FastAPI
from pydantic import BaseModel

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

MODELS_DIR = Path(os.getenv("MODELS_DIR", "/models"))
STEP_SECONDS = int(os.getenv("STEP_SECONDS", "60"))  # must match the bucket size chclient.QueryRawValues uses
HORIZON_STEPS = {"short": 10, "long": 30}
MIN_CONTEXT_POINTS = 5

app = FastAPI()


class IntervalsRequest(BaseModel):
    channel: str
    context: list[float]
    horizon: str = "short"


def _load_pipeline():
    try:
        from chronos import ChronosPipeline
    except ImportError:
        log.warning("chronos-forecasting not installed — /intervals will report unavailable")
        return None

    model_path = "amazon/chronos-t5-small"
    meta_path = MODELS_DIR / "chronos_metadata.json"
    finetuned_dir = MODELS_DIR / "chronos_finetuned"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            if meta.get("use_finetuned") and finetuned_dir.exists() and any(finetuned_dir.iterdir()):
                model_path = str(finetuned_dir)
        except Exception as exc:
            log.warning("failed to read %s: %s", meta_path, exc)

    try:
        log.info("loading Chronos pipeline from '%s'", model_path)
        pipeline = ChronosPipeline.from_pretrained(
            model_path, device_map="cpu", torch_dtype=torch.float32,
        )
        log.info("Chronos pipeline ready")
        return pipeline
    except Exception as exc:
        log.warning("Chronos pipeline failed to load (%s) — /intervals will report unavailable", exc)
        return None


_pipeline = _load_pipeline()


@app.post("/intervals")
def intervals(req: IntervalsRequest) -> dict:
    if _pipeline is None or len(req.context) < MIN_CONTEXT_POINTS:
        return {"available": False, "channel": req.channel}

    horizon_steps = HORIZON_STEPS.get(req.horizon, HORIZON_STEPS["short"])
    ctx = torch.tensor(req.context, dtype=torch.float32).unsqueeze(0)  # (1, context_len)

    try:
        with torch.no_grad():
            forecast = _pipeline.predict(ctx, prediction_length=horizon_steps, num_samples=20)
        samples = forecast[0].numpy()  # (num_samples, horizon_steps)
        p10 = np.percentile(samples, 10, axis=0).tolist()
        p50 = np.percentile(samples, 50, axis=0).tolist()
        p90 = np.percentile(samples, 90, axis=0).tolist()
    except Exception as exc:
        log.warning("Chronos predict failed for channel=%s: %s", req.channel, exc)
        return {"available": False, "channel": req.channel}

    now = datetime.now(timezone.utc)
    timestamps = [
        (now + timedelta(seconds=STEP_SECONDS * (i + 1))).isoformat()
        for i in range(horizon_steps)
    ]

    return {
        "available": True,
        "channel": req.channel,
        "p10": p10,
        "p50": p50,
        "p90": p90,
        "horizon": req.horizon,
        "timestamps": timestamps,
    }


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "chronos_available": _pipeline is not None}
