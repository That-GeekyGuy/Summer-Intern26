import numpy as np
import pytest
from feature_eng import extract_features, N_FEATURES, SKLEARN_FEATURE_NAMES

CHANNELS_14 = [
    "pfcp_sessions_total", "port_bytes_N3_rx_rate", "port_bytes_N6_tx_rate",
    "port_pkts_N3_rx_rate", "port_dropped_N3_rx_rate", "port_dropped_N6_rx_rate",
    "go_goroutines", "go_heap_alloc_bytes", "process_cpu_rate",
    "upf_sim_scenario_congestion", "upf_sim_scenario_flatline",
    "upf_sim_scenario_spike", "port_pkts_N6_tx_rate", "port_dropped_N6_rx",
]

_WINDOW = [[float(i + j) for j in range(512)] for i in range(14)]


def test_output_shape():
    vec = extract_features(_WINDOW, CHANNELS_14)
    assert len(vec) == N_FEATURES


def test_output_is_finite():
    vec = extract_features(_WINDOW, CHANNELS_14)
    assert all(np.isfinite(v) for v in vec)


def test_feature_names_length():
    assert len(SKLEARN_FEATURE_NAMES) == N_FEATURES


def test_zero_window_produces_finite():
    zero = [[0.0] * 512 for _ in range(14)]
    vec = extract_features(zero, CHANNELS_14)
    assert len(vec) == N_FEATURES
    assert all(np.isfinite(v) for v in vec)


def test_missing_channel_graceful():
    short_channels = CHANNELS_14[:5]
    short_window = _WINDOW[:5]
    vec = extract_features(short_window, short_channels)
    assert len(vec) == N_FEATURES
    assert all(np.isfinite(v) for v in vec)


def test_nan_input_produces_finite():
    nan_window = [[float("nan")] * 512 for _ in range(14)]
    vec = extract_features(nan_window, CHANNELS_14)
    assert len(vec) == N_FEATURES
    assert all(np.isfinite(v) for v in vec), "NaN input must yield finite output (guarded to 0.0)"
