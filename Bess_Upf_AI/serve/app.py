import json
import logging
import os
from pathlib import Path

import numpy as np
from fastapi import FastAPI
from pydantic import BaseModel

try:
    import torch
    _TORCH_AVAILABLE = True
except ImportError:
    _TORCH_AVAILABLE = False

try:
    import ray  # noqa: F401 — availability probe, only `serve` submodule used below
    from ray import serve
    _RAY_AVAILABLE = True
except ImportError:
    _RAY_AVAILABLE = False
    # ponytail: no-op stubs so sklearn unit tests run without ray installed
    class _NoopServe:
        @staticmethod
        def deployment(**_kw):
            return lambda cls: cls
        @staticmethod
        def ingress(_app):
            return lambda cls: cls
        @staticmethod
        def bind(*_a, **_kw):
            return None
    serve = _NoopServe()  # type: ignore[assignment]

from feature_eng import extract_features

log = logging.getLogger(__name__)

MODELS_DIR = Path(os.getenv("MODELS_DIR", "/models"))
SHADOW_TOPIC = os.getenv("SHADOW_TOPIC", "upf.shadow.detections")
_IF_THRESHOLD = -1e-16  # overridden from metadata.json

app = FastAPI()


class DetectRequest(BaseModel):
    channels: list[list[float]]
    channel_names: list[str] | None = None
    upf_id: str = "unknown"
    ts: float = 0.0


class ChronosForecastRequest(BaseModel):
    context: list[float]
    timestamps: list[str] | None = None


@serve.deployment(num_replicas=2, ray_actor_options={"num_gpus": 0})
@serve.ingress(app)
class MLServeDeployment:
    def __init__(self):
        self.device = "cuda" if _TORCH_AVAILABLE and torch.cuda.is_available() else "cpu"
        self._load_sklearn_models()
        self._load_moment_models()
        self._load_metadata()
        self._shadow = self._init_shadow()
        log.info(
            "Serve ready: sklearn=%s moment=%s device=%s",
            self.sklearn_available, self.moment_available, self.device,
        )

    def _load_metadata(self):
        global _IF_THRESHOLD
        meta_path = MODELS_DIR / "metadata.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            _IF_THRESHOLD = meta.get("thresholds", {}).get(
                "isolation_forest", _IF_THRESHOLD
            )
        self._model_version = f"v2-sklearn@{getattr(self, 'device', 'cpu')}"

    def _load_sklearn_models(self):
        self.sklearn_available = False
        self._scaler = None
        self._if = None
        self._rf = None
        try:
            import joblib
            # joblib.load is safe here: files come from MODELS_DIR, an
            # operator-controlled path (Docker volume / local path), never from
            # user-supplied input. Treat MODELS_DIR as a trust boundary.
            self._scaler = joblib.load(MODELS_DIR / "scaler.joblib")
            self._if = joblib.load(MODELS_DIR / "isolation_forest.joblib")
            self.sklearn_available = True
            rf_path = MODELS_DIR / "random_forest.joblib"
            try:
                self._rf = joblib.load(rf_path) if rf_path.exists() else None
            except Exception as rf_exc:
                log.warning("RF load failed, IF-only mode: %s", rf_exc)
                self._rf = None
            log.info("sklearn models loaded (rf=%s)", self._rf is not None)
        except Exception as exc:
            log.warning("sklearn models not loaded: %s", exc)

    def _load_moment_models(self):
        from moment_loader import load_moment_head
        self._moment_head, self.moment_available = load_moment_head(
            MODELS_DIR / "moment_head.pt",
            MODELS_DIR / "moment_threshold.json",
            self.device,
        )

    def _init_shadow(self):
        from shadow_publisher import ShadowPublisher
        broker = os.getenv("KAFKA_BROKER", "")
        if not broker:
            return ShadowPublisher(producer=None)
        try:
            from confluent_kafka import Producer
            producer = Producer({"bootstrap.servers": broker})
            log.info("shadow publisher connected to %s", broker)
            return ShadowPublisher(producer=producer, topic=SHADOW_TOPIC)
        except Exception as exc:
            log.warning("shadow publisher init failed: %s", exc)
            return ShadowPublisher(producer=None)

    def _detect_impl(self, req: DetectRequest) -> dict:
        ch_names = req.channel_names or []
        channels = req.channels

        anomaly = False
        anomaly_score = 0.0
        if_score = 0.0
        rf_proba = 0.0
        channel_scores: dict = {}

        if self.sklearn_available:
            vec = extract_features(channels, ch_names)
            X = np.array(vec, dtype=np.float64).reshape(1, -1)
            X_scaled = self._scaler.transform(X)

            if_score = float(self._if.decision_function(X_scaled)[0])
            if_anomaly = if_score < _IF_THRESHOLD

            rf_anomaly = False
            if self._rf is not None:
                proba = self._rf.predict_proba(X_scaled)[0]
                rf_proba = float(proba[1]) if len(proba) > 1 else 0.0
                rf_anomaly = rf_proba >= 0.5

            anomaly = if_anomaly or rf_anomaly
            anomaly_score = rf_proba if self._rf is not None else max(0.0, -if_score)
            channel_scores = {"if_score": if_score, "rf_proba": rf_proba}

        moment_score = 0.0
        if self.moment_available and self._moment_head is not None:
            from moment_loader import score_moment
            moment_score, moment_anomaly = score_moment(
                self._moment_head, channels, ch_names, self.device
            )
            if moment_anomaly:
                anomaly = True
            channel_scores["moment_score"] = moment_score

        return {
            "available": self.sklearn_available or self.moment_available,
            "anomaly": anomaly,
            "anomaly_score": anomaly_score,
            "if_score": if_score,
            "rf_proba": rf_proba,
            "moment_score": moment_score,
            "threshold": 0.5,
            "channel_scores": channel_scores,
            "top_anomalous_channels": [],
            "confidence": anomaly_score,
            "model_version": self._model_version,
        }

    @app.post("/detect")
    def detect(self, req: DetectRequest) -> dict:
        result = self._detect_impl(req)
        self._shadow.publish(upf_id=req.upf_id, ts=req.ts, result=result)
        return result

    @app.post("/detect_batch")
    def detect_batch(self, reqs: list[DetectRequest]) -> list[dict]:
        results = []
        for req in reqs:
            res = self._detect_impl(req)
            self._shadow.publish(upf_id=req.upf_id, ts=req.ts, result=res)
            results.append(res)
        return results

    @app.post("/forecast")
    def forecast(self, req: ChronosForecastRequest) -> dict:
        return {
            "available": False,
            "forecast_median": [0.0] * 10,
            "forecast_p10": [0.0] * 10,
            "forecast_p90": [0.0] * 10,
            "forecast_timestamps": [],
            "capacity_breach_eta_minutes": None,
            "breach_probability": 0.0,
            "trend": "flat",
            "model_version": self._model_version,
        }

    @app.get("/health")
    def health(self) -> dict:
        return {
            "status": "ok",
            "device": self.device,
            "sklearn": self.sklearn_available,
            "moment": self.moment_available,
        }


app_node = MLServeDeployment.bind() if _RAY_AVAILABLE else None
