# serve/tests/test_shadow.py
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import json
from unittest.mock import MagicMock


def test_shadow_publisher_sends_to_kafka():
    from shadow_publisher import ShadowPublisher
    mock_producer = MagicMock()
    pub = ShadowPublisher(producer=mock_producer, topic="upf.shadow.detections")
    pub.publish(
        upf_id="sim-upf-0",
        ts=1234567890.0,
        result={
            "anomaly": False,
            "anomaly_score": 0.1,
            "if_score": 0.02,
            "rf_proba": 0.1,
            "moment_score": 0.0,
            "model_version": "v2-sklearn@cpu",
        },
    )
    mock_producer.produce.assert_called_once()
    payload = json.loads(mock_producer.produce.call_args[1]["value"])
    assert payload["upf_id"] == "sim-upf-0"
    assert "anomaly_score" in payload
    assert "if_score" in payload


def test_shadow_publisher_noop_when_no_producer():
    from shadow_publisher import ShadowPublisher
    pub = ShadowPublisher(producer=None, topic="upf.shadow.detections")
    pub.publish(upf_id="x", ts=0.0, result={})  # must not raise
