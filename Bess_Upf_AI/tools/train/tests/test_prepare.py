"""
Unit tests for prepare_dataset.py (Tier 2 + TSFM).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# Allow importing the train module without installing it
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from train.prepare_dataset import (
    RESAMPLE_SECONDS,
    FFILL_SLOW_STEPS,
    MOMENT_SEQ_LEN,
    MOMENT_STRIDE,
    resample_and_rate,
    ffill_slow_channels,
    compute_binary_label,
    build_moment_windows,
)

# ── Resample and rate ─────────────────────────────────────────────────────────

def test_resample_and_rate_normal():
    """Test 15s resampling and counter to rate conversion."""
    times = pd.date_range("2026-06-22 10:00:00", periods=5, freq="5s", tz="UTC")
    df = pd.DataFrame({
        "timestamp": times,
        "port_bytes_count{dir=rx_iface=N3}": [0.0, 50.0, 100.0, 150.0, 200.0], # counter
        "tsi_value": [1.0, 1.2, 1.4, 1.6, 1.8] # gauge
    })
    
    wide = resample_and_rate(df)
    
    # 5s data over 20s total will result in 2 rows at 15s cadence (00 and 15)
    assert len(wide) == 2
    
    # First diff is NaN
    assert np.isnan(wide["port_bytes_count{dir=rx_iface=N3}"].iloc[0])
    
    # Second row rate = (last value at 15s - last value at 00s) / 15
    # The last value at bucket 10:00:00 (covers 00, 05, 10) is 100
    # The last value at bucket 10:00:15 (covers 15, 20) is 200
    # Difference is 100, over 15 seconds = 100/15
    assert wide["port_bytes_count{dir=rx_iface=N3}"].iloc[1] == pytest.approx(100.0 / 15.0)


# ── Forward fill ──────────────────────────────────────────────────────────────

def test_ffill_slow_channels():
    """Test forward filling for slow cadence columns."""
    df = pd.DataFrame({
        "timestamp": pd.date_range("2026-06-22 10:00:00", periods=10, freq="15s", tz="UTC"),
        "tsi_value": [1.0, np.nan, np.nan, np.nan, np.nan, np.nan, np.nan, np.nan, np.nan, 2.0]
    })
    
    ffilled = ffill_slow_channels(df)
    
    # Should fill up to FFILL_SLOW_STEPS (6)
    assert ffilled["tsi_value"].iloc[0] == 1.0
    assert ffilled["tsi_value"].iloc[6] == 1.0
    assert np.isnan(ffilled["tsi_value"].iloc[7]) # Limit exceeded
    assert ffilled["tsi_value"].iloc[9] == 2.0


# ── Binary label ──────────────────────────────────────────────────────────────

def test_compute_binary_label():
    """Threshold should be 75th percentile of the training set only."""
    df = pd.DataFrame({
        "timestamp": pd.date_range("2026-06-22 10:00:00", periods=10, freq="15s", tz="UTC"),
        "tsi_value": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    })
    
    split_ts = df["timestamp"].iloc[5] # First 5 rows are training
    
    # Training set tsi_value: [1.0, 2.0, 3.0, 4.0, 5.0]
    # 75th percentile = 4.0
    binary, threshold = compute_binary_label(df, "tsi_value", split_ts)
    
    assert threshold == 4.0
    assert binary.iloc[0] == 0
    assert binary.iloc[3] == 0
    assert binary.iloc[4] == 1 # 5.0 > 4.0
    assert binary.iloc[9] == 1


# ── MOMENT windows ────────────────────────────────────────────────────────────

def test_build_moment_windows_too_short():
    """Should return empty arrays if dataset is shorter than seq_len."""
    df = pd.DataFrame({
        "timestamp": pd.date_range("2026-06-22 10:00:00", periods=10, freq="15s", tz="UTC"),
        "tsi_value": [1.0] * 10,
        "uoi_binary": [0] * 10,
    })
    
    channel_map = {"tsi_value": ("tsi_value",)}
    
    windows, labels, names = build_moment_windows(
        df, channel_map, "uoi_binary", seq_len=MOMENT_SEQ_LEN, stride=MOMENT_STRIDE
    )
    
    assert len(windows) == 0
    assert len(labels) == 0


def test_build_moment_windows_valid():
    """Test sliding window generation."""
    seq_len = 10
    stride = 5
    n_rows = 20
    df = pd.DataFrame({
        "timestamp": pd.date_range("2026-06-22 10:00:00", periods=n_rows, freq="15s", tz="UTC"),
        "ch1_name": [1.0] * n_rows,
        "uoi_binary": [0] * (n_rows - 1) + [1], # last item is anomaly
    })
    
    channel_map = {"ch1": ("ch1_name",)}
    
    windows, labels, names = build_moment_windows(
        df, channel_map, "uoi_binary", seq_len=seq_len, stride=stride
    )
    
    # 20 rows, seq_len=10, stride=5 -> 3 windows (0-10, 5-15, 10-20)
    assert len(windows) == 3
    assert windows.shape == (3, 1, 10)
    assert names == ["ch1"]
    
    # Labels should match the last element of each window
    assert labels[0] == 0 # label of row 9
    assert labels[1] == 0 # label of row 14
    assert labels[2] == 1 # label of row 19
