import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import json
import numpy as np
import pytest
import torch


def _make_fake_head(tmp_path, embedding_dim=7168):
    import torch.nn as nn
    head = nn.Linear(embedding_dim, 1)
    torch.save(head.state_dict(), tmp_path / "moment_head.pt")
    (tmp_path / "moment_threshold.json").write_text(json.dumps({
        "threshold": 0.5,
        "normal_mean": 0.1,
        "normal_std": 0.2,
        "embedding_dim": embedding_dim,
    }))

CHANNELS_14 = [
    "pfcp_sessions_total", "port_bytes_N3_rx_rate", "port_bytes_N6_tx_rate",
    "port_pkts_N3_rx_rate", "port_dropped_N3_rx_rate", "port_dropped_N6_rx_rate",
    "go_goroutines", "go_heap_alloc_bytes", "process_cpu_rate",
    "upf_sim_scenario_congestion", "upf_sim_scenario_flatline",
    "upf_sim_scenario_spike", "port_pkts_N6_tx_rate", "port_dropped_N6_rx",
]

def test_load_moment_head_succeeds(tmp_path):
    _make_fake_head(tmp_path)
    from moment_loader import load_moment_head
    head, available = load_moment_head(
        tmp_path / "moment_head.pt",
        tmp_path / "moment_threshold.json",
        "cpu",
    )
    assert available
    assert head is not None

def test_load_moment_head_missing_file(tmp_path):
    from moment_loader import load_moment_head
    head, available = load_moment_head(
        tmp_path / "nonexistent.pt",
        tmp_path / "nonexistent.json",
        "cpu",
    )
    assert not available
    assert head is None

def test_score_moment_returns_finite(tmp_path):
    _make_fake_head(tmp_path)
    from moment_loader import load_moment_head, score_moment
    head, available = load_moment_head(
        tmp_path / "moment_head.pt",
        tmp_path / "moment_threshold.json",
        "cpu",
    )
    assert available
    channels = [list(np.random.rand(512)) for _ in range(14)]
    score, is_anomaly = score_moment(head, channels, CHANNELS_14, "cpu")
    assert np.isfinite(score)
    assert isinstance(is_anomaly, bool)
