import pytest
from mitigation.policy import (
    ActionClass, TrustLevel, classify_anomaly, MIN_CONFIDENCE
)

def test_low_score_returns_no_action():
    event = {"anomaly_score": 0.3, "top_anomalous_channels": ["pfcp_sessions_total"]}
    action, trust = classify_anomaly(event)
    assert action == ActionClass.NO_ACTION
    assert trust == TrustLevel.OBSERVE

def test_drop_channels_map_to_xdp():
    event = {"anomaly_score": 0.9, "top_anomalous_channels": ["port_dropped_N6_rx_rate"]}
    action, trust = classify_anomaly(event)
    assert action == ActionClass.XDP_RATE_LIMIT
    assert trust == TrustLevel.OBSERVE

def test_pfcp_channels_map_to_reroute():
    event = {"anomaly_score": 0.8, "top_anomalous_channels": ["pfcp_sessions_total"]}
    action, trust = classify_anomaly(event)
    assert action == ActionClass.PFCP_REROUTE
    assert trust == TrustLevel.OBSERVE

def test_unknown_channels_map_to_hpa():
    event = {"anomaly_score": 0.75, "top_anomalous_channels": ["some_other_channel"]}
    action, trust = classify_anomaly(event)
    assert action == ActionClass.HPA_SCALE_UP
    assert trust == TrustLevel.OBSERVE

def test_missing_channels_defaults_to_hpa():
    event = {"anomaly_score": 0.9}
    action, trust = classify_anomaly(event)
    assert action == ActionClass.HPA_SCALE_UP
    assert trust == TrustLevel.OBSERVE

def test_boundary_score_exactly_min_confidence():
    event = {"anomaly_score": MIN_CONFIDENCE, "top_anomalous_channels": []}
    action, trust = classify_anomaly(event)
    assert action == ActionClass.HPA_SCALE_UP
    assert trust == TrustLevel.OBSERVE

def test_none_score_returns_no_action():
    event = {"anomaly_score": None, "top_anomalous_channels": ["pfcp_sessions_total"]}
    action, trust = classify_anomaly(event)
    assert action == ActionClass.NO_ACTION
