import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import json
import numpy as np
import pytest

CHANNELS_14 = [
    "pfcp_sessions_total", "port_bytes_N3_rx_rate", "port_bytes_N6_tx_rate",
    "port_pkts_N3_rx_rate", "port_dropped_N3_rx_rate", "port_dropped_N6_rx_rate",
    "go_goroutines", "go_heap_alloc_bytes", "process_cpu_rate",
    "upf_sim_scenario_congestion", "upf_sim_scenario_flatline",
    "upf_sim_scenario_spike", "port_pkts_N6_tx_rate", "port_dropped_N6_rx",
]

def _normal_window():
    rng = np.random.default_rng(0)
    return rng.random((14, 512)).tolist()

def _make_fake_models(tmp_path):
    import joblib
    from sklearn.ensemble import IsolationForest, RandomForestClassifier
    from sklearn.preprocessing import StandardScaler
    from feature_eng import N_FEATURES
    X = np.random.rand(20, N_FEATURES)
    scaler = StandardScaler()
    scaler.fit(X)
    rf = RandomForestClassifier(n_estimators=2, random_state=0)
    rf.fit(X, [0]*10 + [1]*10)
    if_ = IsolationForest(n_estimators=2, random_state=0)
    if_.fit(X)
    joblib.dump(scaler, tmp_path / "scaler.joblib")
    joblib.dump(if_,   tmp_path / "isolation_forest.joblib")
    joblib.dump(rf,    tmp_path / "random_forest.joblib")
    (tmp_path / "metadata.json").write_text(json.dumps({
        "thresholds": {"isolation_forest": 0.0}
    }))

def test_sklearn_models_load(tmp_path, monkeypatch):
    _make_fake_models(tmp_path)
    monkeypatch.setenv("MODELS_DIR", str(tmp_path))
    monkeypatch.setenv("KAFKA_BROKER", "")

    from importlib import reload
    import app as serve_app
    reload(serve_app)

    inst = serve_app.MLServeDeployment.__new__(serve_app.MLServeDeployment)
    inst._load_sklearn_models()
    assert inst.sklearn_available

def test_detect_returns_real_score(tmp_path, monkeypatch):
    _make_fake_models(tmp_path)
    monkeypatch.setenv("MODELS_DIR", str(tmp_path))
    monkeypatch.setenv("KAFKA_BROKER", "")

    from importlib import reload
    import app as serve_app
    reload(serve_app)

    inst = serve_app.MLServeDeployment.__new__(serve_app.MLServeDeployment)
    inst._load_sklearn_models()
    inst._load_metadata()
    inst.moment_available = False
    inst._moment_head = None
    from shadow_publisher import ShadowPublisher
    inst._shadow = ShadowPublisher(producer=None)

    from app import DetectRequest
    req = DetectRequest(channels=_normal_window(), channel_names=CHANNELS_14)
    result = inst._detect_impl(req)

    assert "anomaly_score" in result
    assert "if_score" in result
    assert "rf_proba" in result
    assert isinstance(result["anomaly"], bool)
    assert result["if_score"] != 0.0, "IF score must be real model output, not stub zero"
