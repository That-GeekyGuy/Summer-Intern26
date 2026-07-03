# serve/shadow_publisher.py
import json
import logging
import os
from typing import Any

log = logging.getLogger(__name__)

SHADOW_TOPIC = os.getenv("SHADOW_TOPIC", "upf.shadow.detections")


class ShadowPublisher:
    def __init__(self, producer=None, topic: str = SHADOW_TOPIC):
        self._producer = producer
        self._topic = topic

    def publish(self, upf_id: str, ts: float, result: dict[str, Any]) -> None:
        if self._producer is None:
            return
        record = {
            "upf_id":        upf_id,
            "ts":            ts,
            "anomaly":       int(bool(result.get("anomaly", False))),
            "anomaly_score": result.get("anomaly_score", 0.0),
            "if_score":      result.get("if_score", 0.0),
            "rf_proba":      result.get("rf_proba", 0.0),
            "moment_score":  result.get("moment_score", 0.0),
            "model_version": result.get("model_version", ""),
        }
        try:
            self._producer.produce(
                self._topic,
                key=upf_id.encode(),
                value=json.dumps(record, allow_nan=False).encode(),
            )
            self._producer.poll(0)
        except Exception as exc:
            log.warning("shadow publish failed: %s", exc)
