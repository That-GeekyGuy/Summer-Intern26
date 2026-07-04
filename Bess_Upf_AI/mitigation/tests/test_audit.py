import json
import pytest
from unittest.mock import MagicMock
from mitigation.audit import ActionAuditRecord, AuditPublisher, AUDIT_TOPIC
from mitigation.policy import ActionClass, TrustLevel


def _make_record(**overrides) -> ActionAuditRecord:
    defaults = dict(
        action_id="aid-1",
        upf_id="upf-1",
        ts=1.0,
        action_class=ActionClass.HPA_SCALE_UP,
        trust_level=TrustLevel.OBSERVE,
        dry_run=True,
        success=True,
        message="ok",
        anomaly_score=0.9,
        model_version="v1",
        rollback_token="tok",
    )
    defaults.update(overrides)
    return ActionAuditRecord(**defaults)


def test_publish_sends_to_correct_topic():
    producer = MagicMock()
    pub = AuditPublisher(producer=producer, topic=AUDIT_TOPIC)
    pub.publish(_make_record())
    producer.produce.assert_called_once()
    call_kwargs = producer.produce.call_args
    assert call_kwargs[1].get("topic") == AUDIT_TOPIC or call_kwargs[0][0] == AUDIT_TOPIC


def test_publish_serializes_booleans_as_int():
    producer = MagicMock()
    pub = AuditPublisher(producer=producer)
    pub.publish(_make_record(dry_run=True, success=False))
    raw = producer.produce.call_args[1].get("value") or producer.produce.call_args[0][1]
    payload = json.loads(raw)
    assert payload["dry_run"] == 1
    assert payload["success"] == 0


def test_publish_no_nan_in_payload():
    producer = MagicMock()
    pub = AuditPublisher(producer=producer)
    pub.publish(_make_record(anomaly_score=float("nan")))
    raw = producer.produce.call_args[1].get("value") or producer.produce.call_args[0][1]
    assert b"NaN" not in raw


def test_publish_swallows_producer_exception():
    producer = MagicMock()
    producer.produce.side_effect = Exception("kafka down")
    pub = AuditPublisher(producer=producer)
    pub.publish(_make_record())  # must not raise
