import json
import pytest
from unittest.mock import MagicMock, patch
from mitigation.policy import ActionClass, TrustLevel, TRUST_LEVELS
from mitigation.guardrails import GuardrailsEngine, RateLimiter, BlastRadiusGuard
from mitigation.audit import AuditPublisher
from mitigation.approval import ApprovalStore
from mitigation.app import _handle_event


def _make_event(**overrides):
    base = {
        "upf_id": "upf-1",
        "ts": 1.0,
        "anomaly_score": 0.9,
        "top_anomalous_channels": ["some_channel"],
        "model_version": "v1",
    }
    base.update(overrides)
    return base


def _make_engine():
    return GuardrailsEngine(
        RateLimiter(max_per_window=10, window_seconds=300.0),
        BlastRadiusGuard(max_upfs=10, window_seconds=300.0),
    )


def test_observe_runs_dry_run():
    producer = MagicMock()
    audit = AuditPublisher(producer=producer)
    store = ApprovalStore()
    engine = _make_engine()

    with patch.dict(TRUST_LEVELS, {ActionClass.HPA_SCALE_UP: TrustLevel.OBSERVE}):
        _handle_event(_make_event(), engine, audit, store)

    producer.produce.assert_called_once()
    d = json.loads(producer.produce.call_args[1].get("value") or producer.produce.call_args[0][1])
    assert d["dry_run"] == 1


def test_approve_queues_in_store():
    audit = AuditPublisher(producer=None)
    store = ApprovalStore()
    engine = _make_engine()

    with patch.dict(TRUST_LEVELS, {ActionClass.HPA_SCALE_UP: TrustLevel.APPROVE}):
        _handle_event(_make_event(), engine, audit, store)

    assert len(store.list_pending()) == 1


def test_auto_blocked_by_guardrails():
    producer = MagicMock()
    audit = AuditPublisher(producer=producer)
    store = ApprovalStore()
    engine = GuardrailsEngine(
        RateLimiter(max_per_window=0, window_seconds=300.0),
        BlastRadiusGuard(max_upfs=10, window_seconds=300.0),
    )

    with patch.dict(TRUST_LEVELS, {ActionClass.HPA_SCALE_UP: TrustLevel.AUTO}):
        _handle_event(_make_event(), engine, audit, store)

    producer.produce.assert_not_called()


def test_auto_executes_and_audits():
    producer = MagicMock()
    audit = AuditPublisher(producer=producer)
    store = ApprovalStore()
    engine = _make_engine()

    with patch.dict(TRUST_LEVELS, {ActionClass.HPA_SCALE_UP: TrustLevel.AUTO}):
        _handle_event(_make_event(), engine, audit, store)

    producer.produce.assert_called_once()
    d = json.loads(producer.produce.call_args[1].get("value") or producer.produce.call_args[0][1])
    assert d["dry_run"] == 0
    assert d["trust_level"] == "auto"
