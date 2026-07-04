import json
import pytest
from unittest.mock import MagicMock
from mitigation.policy import ActionClass, TrustLevel, TRUST_LEVELS
from mitigation.guardrails import GuardrailsEngine, RateLimiter, BlastRadiusGuard
from mitigation.audit import AuditPublisher
from mitigation.approval import ApprovalStore
from mitigation.app import _handle_event


def _engine(max_rate=3, max_upfs=5):
    return GuardrailsEngine(
        RateLimiter(max_per_window=max_rate, window_seconds=300.0),
        BlastRadiusGuard(max_upfs=max_upfs, window_seconds=300.0),
    )


def test_hpa_is_auto():
    assert TRUST_LEVELS[ActionClass.HPA_SCALE_UP] == TrustLevel.AUTO


def test_hpa_auto_full_path():
    producer = MagicMock()
    audit = AuditPublisher(producer=producer)
    event = {"upf_id": "upf-sim-1", "ts": 1.0, "anomaly_score": 0.85,
             "top_anomalous_channels": ["cpu_usage"], "model_version": "v1"}
    _handle_event(event, _engine(), audit, ApprovalStore())

    producer.produce.assert_called_once()
    d = json.loads(producer.produce.call_args[1].get("value") or producer.produce.call_args[0][1])
    assert d["dry_run"] == 0
    assert d["trust_level"] == "auto"
    assert d["action_class"] == "hpa_scale_up"
    assert d["success"] == 1


def test_xdp_still_observe():
    assert TRUST_LEVELS[ActionClass.XDP_RATE_LIMIT] == TrustLevel.OBSERVE


def test_blast_radius_caps_at_5():
    producer = MagicMock()
    audit = AuditPublisher(producer=producer)
    engine = _engine()
    template = {"ts": 1.0, "anomaly_score": 0.9,
                "top_anomalous_channels": ["cpu_usage"], "model_version": "v1"}
    for i in range(6):
        _handle_event({**template, "upf_id": f"upf-{i}"}, engine, audit, ApprovalStore())

    assert producer.produce.call_count == 5
