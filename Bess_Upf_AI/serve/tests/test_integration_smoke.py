"""
Integration smoke: verify serve loads real models and returns non-stub scores.
Skips if real model files not present at MODELS_DIR.
Run: MODELS_DIR=../models python -m pytest tests/test_integration_smoke.py -v
"""
import os
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

MODELS_DIR = Path(os.environ.get("MODELS_DIR", "../../models"))
REQUIRED = ["isolation_forest.joblib", "scaler.joblib"]

pytestmark = pytest.mark.skipif(
    not all((MODELS_DIR / f).exists() for f in REQUIRED),
    reason=f"Real models not found at {MODELS_DIR}",
)

ML_CHANNELS = [
    "pfcp_sessions_total", "port_bytes_N3_rx_rate", "port_bytes_N6_tx_rate",
    "port_pkts_N3_rx_rate", "port_dropped_N3_rx_rate", "port_dropped_N6_rx_rate",
    "go_goroutines", "go_heap_alloc_bytes", "process_cpu_rate",
    "upf_sim_scenario_congestion", "upf_sim_scenario_flatline",
    "upf_sim_scenario_spike", "port_pkts_N6_tx_rate", "port_dropped_N6_rx",
]


def _make_inst():
    os.environ["MODELS_DIR"] = str(MODELS_DIR)
    os.environ["KAFKA_BROKER"] = ""

    from importlib import reload
    import app as serve_app
    reload(serve_app)

    inst = serve_app.MLServeDeployment.__new__(serve_app.MLServeDeployment)
    inst._load_sklearn_models()
    inst._load_moment_models()
    inst._load_metadata()
    from shadow_publisher import ShadowPublisher
    inst._shadow = ShadowPublisher(producer=None)
    return inst, serve_app


@pytest.fixture(scope="module")
def smoke_inst():
    return _make_inst()


def test_health_shows_sklearn_true(smoke_inst):
    inst, _ = smoke_inst
    h = inst.health()
    assert h["sklearn"] is True


def test_detect_real_models_direct(smoke_inst):
    inst, serve_app = smoke_inst
    channels = [[float(i % 50 + 1) for _ in range(512)] for i in range(14)]
    from app import DetectRequest
    req = DetectRequest(channels=channels, channel_names=ML_CHANNELS)
    result = inst._detect_impl(req)

    assert result["available"] is True
    assert isinstance(result["anomaly"], bool)
    assert result["if_score"] != 0.0, "if_score is stub zero — sklearn not loaded"
    assert result["anomaly_score"] >= 0.0
    assert "v2" in result["model_version"]
