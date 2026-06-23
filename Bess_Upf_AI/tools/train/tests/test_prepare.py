"""
Unit tests for prepare_dataset.py.

Run inside the tools Docker image:
    docker run --rm bess-ml-tools python -m pytest train/tests/ -v

These tests do NOT contact MinIO — they test feature engineering and label logic
with synthetic in-memory data.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# Allow importing the train module without installing it
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from train.prepare_dataset import (
    SCRAPE_DT,
    TRANSITION_N,
    _assign_labels,
    _counter_rate,
    _gauge_rate,
    _pivot_scenario,
    ROLLING_WINDOW,
    FEATURE_COLS,
)


# ── Counter rate ──────────────────────────────────────────────────────────────

def test_counter_rate_normal():
    """Rate is (diff / SCRAPE_DT), positive increments produce positive rates."""
    s = pd.Series([0.0, 150.0, 300.0, 450.0])
    rate = _counter_rate(s)
    # First value is NaN (no prior sample)
    assert np.isnan(rate.iloc[0])
    # Subsequent rates should be 10.0 (150 bytes / 15 s)
    assert rate.iloc[1] == pytest.approx(10.0)
    assert rate.iloc[2] == pytest.approx(10.0)
    assert rate.iloc[3] == pytest.approx(10.0)


def test_counter_rate_reset_becomes_nan():
    """Counter reset (decrease) must produce NaN, not a negative rate."""
    s = pd.Series([1000.0, 1500.0, 200.0, 700.0])
    rate = _counter_rate(s)
    assert np.isnan(rate.iloc[0])   # first diff always NaN
    assert rate.iloc[1] == pytest.approx((1500.0 - 1000.0) / SCRAPE_DT)
    assert np.isnan(rate.iloc[2])   # reset: 1500 → 200 must be NaN, not -86.67
    assert rate.iloc[3] == pytest.approx((700.0 - 200.0) / SCRAPE_DT)


def test_counter_rate_zero_increment():
    """Zero increment (flatline counter) should produce rate of 0.0."""
    s = pd.Series([500.0, 500.0, 500.0])
    rate = _counter_rate(s)
    assert np.isnan(rate.iloc[0])
    assert rate.iloc[1] == pytest.approx(0.0)
    assert rate.iloc[2] == pytest.approx(0.0)


def test_gauge_rate_can_be_negative():
    """Gauge rate allows negative values (sessions dropping is valid)."""
    s = pd.Series([12000.0, 11000.0, 10000.0])
    rate = _gauge_rate(s)
    assert np.isnan(rate.iloc[0])
    assert rate.iloc[1] == pytest.approx(-1000.0 / SCRAPE_DT)
    assert rate.iloc[2] == pytest.approx(-1000.0 / SCRAPE_DT)


# ── Rolling window ────────────────────────────────────────────────────────────

def test_rolling_window_is_20_samples():
    """5-minute window at 15-second cadence must be exactly 20 samples."""
    assert ROLLING_WINDOW == 20, f"expected 20, got {ROLLING_WINDOW}"


def test_rolling_mean_correct_width():
    """Rolling mean should use the last 20 samples."""
    s = pd.Series([float(i) for i in range(50)])
    roll_mean = s.rolling(window=ROLLING_WINDOW, min_periods=1).mean()
    # At index 19 (20th sample), mean should be mean(0..19) = 9.5
    assert roll_mean.iloc[19] == pytest.approx(9.5)
    # At index 25, mean should be mean(6..25) = 15.5
    assert roll_mean.iloc[25] == pytest.approx(15.5)


# ── Transition exclusion ──────────────────────────────────────────────────────

def _make_scenario_df(modes_by_ts: dict) -> pd.Series:
    """Build a mode Series indexed by ts_sec."""
    return pd.Series(modes_by_ts)


def test_transition_rows_excluded():
    """Rows within TRANSITION_N samples of a scenario boundary get label='transition'."""
    ts_range = list(range(0, 600, SCRAPE_DT))   # 40 timestamps
    wide = pd.DataFrame(index=ts_range)
    # Scenario switches at ts=300 (index 20)
    modes = {t: ("session_spike" if t >= 300 else "normal") for t in ts_range}
    mode_series = pd.Series(modes)

    labels = _assign_labels(wide, mode_series)

    # At index 20 (ts=300), mode changes → should be 'transition'
    assert labels[300] == "transition"
    # TRANSITION_N samples before: ts = 300 - TRANSITION_N*15
    boundary = 300
    for offset in range(TRANSITION_N + 1):
        ts_before = boundary - offset * SCRAPE_DT
        ts_after  = boundary + offset * SCRAPE_DT
        if ts_before in labels.index:
            assert labels[ts_before] == "transition", f"ts={ts_before} should be transition"
        if ts_after in labels.index:
            assert labels[ts_after] == "transition", f"ts={ts_after} should be transition"

    # Far from boundary: should be normal or session_spike, not transition
    assert labels[0] == "normal"
    assert labels[ts_range[-1]] == "session_spike"


def test_transition_label_applied_symmetrically():
    """TRANSITION_N exclusion applies equally before and after the boundary."""
    ts_range = list(range(0, 1200, SCRAPE_DT))  # 80 timestamps
    wide = pd.DataFrame(index=ts_range)
    # Mode switches at ts=600 (midpoint)
    modes = {t: ("flatline" if t >= 600 else "normal") for t in ts_range}
    labels = _assign_labels(wide, pd.Series(modes))

    n = TRANSITION_N
    boundary = 600
    for offset in range(1, n + 1):
        ts_before = boundary - offset * SCRAPE_DT
        ts_after  = boundary + offset * SCRAPE_DT
        assert labels.get(ts_before) == "transition", f"before: ts={ts_before}"
        assert labels.get(ts_after)  == "transition", f"after: ts={ts_after}"

    # One sample beyond exclusion zone should NOT be transition
    just_before = boundary - (n + 1) * SCRAPE_DT
    just_after  = boundary + (n + 1) * SCRAPE_DT
    assert labels.get(just_before) == "normal"
    assert labels.get(just_after)  == "flatline"


# ── Scenario pivot ────────────────────────────────────────────────────────────

def _make_raw_scenario_df(ts_mode_pairs: list[tuple]) -> pd.DataFrame:
    """Build the raw DataFrame that _pivot_scenario expects."""
    rows = []
    for ts, mode, val in ts_mode_pairs:
        rows.append({
            "ts_sec": ts,
            "labels_dict": {"__name__": "upf_sim_scenario", "mode": mode, "job": "upf"},
            "value": val,
        })
    return pd.DataFrame(rows)


def test_pivot_scenario_returns_active_mode():
    """The active mode (value==1) should be returned for each timestamp."""
    raw = _make_raw_scenario_df([
        (100, "normal", 1.0),
        (100, "session_spike", 0.0),
        (115, "normal", 0.0),
        (115, "session_spike", 1.0),
    ])
    result = _pivot_scenario(raw)
    assert result[100] == "normal"
    assert result[115] == "session_spike"


def test_pivot_scenario_missing_ts_defaults_to_normal():
    """Timestamps with no active mode (all zeros) default to 'normal' in _assign_labels."""
    raw = _make_raw_scenario_df([
        (200, "normal", 0.0),
        (200, "session_spike", 0.0),
    ])
    result = _pivot_scenario(raw)
    # No mode has value==1, so ts=200 should not appear in result
    assert 200 not in result.index


# ── Feature column list ───────────────────────────────────────────────────────

def test_feature_count():
    """Total feature column count must be 39 (7 rate + 4 ratio + 28 rolling)."""
    assert len(FEATURE_COLS) == 39, f"expected 39, got {len(FEATURE_COLS)}"


def test_feature_cols_no_duplicates():
    assert len(FEATURE_COLS) == len(set(FEATURE_COLS)), "duplicate feature names"


def test_feature_cols_include_required():
    required = [
        "rate_pfcp_sessions",
        "rate_bytes_N3_rx", "rate_bytes_N3_tx",
        "rate_bytes_N6_rx", "rate_bytes_N6_tx",
        "rate_drops_N3", "rate_drops_N6",
        "rx_tx_ratio_N3", "rx_tx_ratio_N6",
        "drop_fraction_N3", "drop_fraction_N6",
        "rate_pfcp_sessions_mean_5m", "rate_pfcp_sessions_std_5m",
    ]
    missing = [f for f in required if f not in FEATURE_COLS]
    assert not missing, f"missing features: {missing}"
