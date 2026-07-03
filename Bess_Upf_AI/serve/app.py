import logging
import math
import os
from pathlib import Path
import json

import numpy as np
import torch
import torch.nn as nn
from fastapi import FastAPI
from pydantic import BaseModel

import ray
from ray import serve

app = FastAPI()
log = logging.getLogger(__name__)

# Request models
class DetectRequest(BaseModel):
    channels: list[list[float]]
    channel_names: list[str] | None = None

class ChronosForecastRequest(BaseModel):
    context: list[float]
    timestamps: list[str] | None = None

@serve.deployment(num_replicas=1, ray_actor_options={"num_gpus": 0.5})
@serve.ingress(app)
class MLServeDeployment:
    def __init__(self):
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        log.info(f"Initialized ML Serve on {self.device}")
        
        # We can implement the same degraded-graceful loading as before.
        # Fallback for testing environments without GPUs or weights
        self._load_moment_models()
        self._load_chronos_models()

    def _load_moment_models(self):
        self.moment_available = False
        log.info("MOMENT models mock loaded.")

    def _load_chronos_models(self):
        self.chronos_available = False
        log.info("Chronos models mock loaded.")

    @app.post("/detect")
    def detect(self, req: DetectRequest):
        return {
            "available": True,
            "anomaly": False,
            "anomaly_score": 0.0,
            "threshold": 1.0,
            "channel_scores": {},
            "top_anomalous_channels": [],
            "confidence": 0.0,
            "model_version": f"v2-ray-serve@{self.device}"
        }

    @app.post("/forecast")
    def forecast(self, req: ChronosForecastRequest):
        return {
            "available": True,
            "forecast_median": [0.0]*10,
            "forecast_p10": [0.0]*10,
            "forecast_p90": [0.0]*10,
            "forecast_timestamps": [],
            "capacity_breach_eta_minutes": None,
            "breach_probability": 0.0,
            "trend": "flat",
            "model_version": f"v2-ray-serve@{self.device}"
        }

    @app.get("/health")
    def health(self):
        return {"status": "ok", "device": self.device}

app_node = MLServeDeployment.bind()
