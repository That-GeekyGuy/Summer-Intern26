"""
Traffic regime classifier.

Classifies each time window as: low / normal / peak / surge
based on multivariate position within the STL seasonal profile for that hour.

Key principle: a 90th-percentile load at 9am Monday is PEAK (expected).
The same load at 3am Sunday is SURGE (unexpected).
This requires per-hour expected distributions, not global thresholds.
"""

from typing import Optional
import numpy as np

try:
    import scipy.stats
    _SCIPY = True
except ImportError:
    _SCIPY = False

# Traffic channels that carry diurnal patterns — used for regime classification.
# Go runtime channels (goroutines, heap) are excluded — they're aperiodic.
REGIME_CHANNELS = [
    "port_bytes_N3_rx_rate",
    "port_bytes_N6_tx_rate",
    "port_pkts_N3_rx_rate",
    "port_dropped_N3_rx_rate",
    "pfcp_sessions_total",
    "uoi_session_component",
    "uoi_throughput_component",
]


def classify_regime(
    current_features: dict[str, float],
    stl_profiles: dict[str, dict],
    hour: int,
    is_holiday: bool = False,
    is_weekend: bool = False,
) -> tuple[str, float]:
    """
    Classify the current traffic window into a regime label.

    Args:
        current_features:  dict {channel_name: current_value}
        stl_profiles:      dict {channel_name: {"hourly_means": {...}, "hourly_stds": {...}}}
        hour:              0-23
        is_holiday:        if True, use weekend profile as fallback (lighter traffic expected)
        is_weekend:        if True, note in context

    Returns:
        (regime, median_percentile)
        regime: "low" | "normal" | "peak" | "surge"
        median_percentile: 0.0-1.0, position within seasonal distribution
    """
    hour_key = str(hour)
    percentiles = []

    # Use weekend profile if holiday and not enough holiday data
    profile_hour = hour_key

    for channel in REGIME_CHANNELS:
        if channel not in current_features:
            continue
        if channel not in stl_profiles:
            continue

        profile = stl_profiles[channel]
        means = profile.get("hourly_means", {})
        stds = profile.get("hourly_stds", {})

        expected_mean = means.get(profile_hour, means.get("0", 0.0))
        expected_std = stds.get(profile_hour, stds.get("0", 1.0))

        if expected_std < 1e-9:
            expected_std = max(abs(expected_mean) * 0.1, 1e-6)

        current = current_features[channel]
        zscore = (current - expected_mean) / expected_std

        if _SCIPY:
            pct = float(scipy.stats.norm.cdf(zscore))
        else:
            # Approximation: logistic CDF
            pct = float(1.0 / (1.0 + np.exp(-1.702 * zscore)))

        percentiles.append(pct)

    if not percentiles:
        return "normal", 0.5

    median_pct = float(np.median(percentiles))

    # Threshold calibration (see prompt spec):
    # < 0.15:       low    — below normal for this hour
    # 0.15 – 0.75:  normal — within expected range
    # 0.75 – 0.95:  peak   — elevated but within seasonal bounds
    # > 0.95:       surge  — above seasonal expectation (unexpected)
    if median_pct < 0.15:
        regime = "low"
    elif median_pct < 0.75:
        regime = "normal"
    elif median_pct < 0.95:
        regime = "peak"
    else:
        regime = "surge"

    return regime, median_pct


def build_hourly_forecast(
    stl_profiles: dict[str, dict],
    data_coverage_days: float,
) -> list[dict]:
    """
    Build a 24-hour expected regime forecast using STL profiles.

    Returns a list of 24 dicts (one per hour 0-23) with:
      hour, regime, confidence, expected_sessions_mean,
      expected_sessions_p10, expected_sessions_p90,
      historical_anomaly_rate, label
    """
    result = []

    session_profile = stl_profiles.get("pfcp_sessions_total", {})
    hourly_means = session_profile.get("hourly_means", {})
    hourly_stds  = session_profile.get("hourly_stds", {})

    # Confidence degrades with less data coverage
    # With < 2 days, we've seen each hour at most twice — confidence is low
    base_confidence = min(1.0, max(0.3, data_coverage_days / 7.0))

    # Historical anomaly rate per hour (from profiles if available)
    anomaly_rates = session_profile.get("hourly_anomaly_rates", {})

    # Identify peak/trough hours from session mean profile
    all_means = {int(h): v for h, v in hourly_means.items()}
    if all_means:
        mean_arr = [all_means.get(h, 0.0) for h in range(24)]
        overall_mean = float(np.mean(mean_arr))
        overall_std  = float(np.std(mean_arr))
    else:
        mean_arr = [0.0] * 24
        overall_mean = 0.0
        overall_std = 1.0

    for hour in range(24):
        h_key = str(hour)
        mean_val = hourly_means.get(h_key, overall_mean)
        std_val  = hourly_stds.get(h_key, overall_std * 0.3)

        # Regime from expected position in 24h distribution
        if overall_std > 1e-9:
            z = (mean_val - overall_mean) / overall_std
        else:
            z = 0.0

        if z < -0.7:
            hour_regime = "low"
            label = "quiet"
        elif z < 0.3:
            hour_regime = "normal"
            label = "normal"
        elif z < 1.0:
            hour_regime = "peak"
            label = "peak"
        else:
            hour_regime = "surge"
            label = "high peak"

        p10 = max(0.0, mean_val - 1.28 * std_val)
        p90 = mean_val + 1.28 * std_val

        result.append({
            "hour":                    hour,
            "regime":                  hour_regime,
            "confidence":              round(base_confidence, 2),
            "expected_sessions_mean":  round(mean_val, 1),
            "expected_sessions_p10":   round(p10, 1),
            "expected_sessions_p90":   round(p90, 1),
            "historical_anomaly_rate": anomaly_rates.get(h_key, 0.02),
            "label":                   label,
        })

    return result


def find_peak_trough_hours(hourly_forecast: list[dict]) -> tuple[list[int], list[int]]:
    """Extract peak and trough hours from hourly forecast."""
    peak_hours   = [h["hour"] for h in hourly_forecast if h["regime"] in ("peak", "surge")]
    trough_hours = [h["hour"] for h in hourly_forecast if h["regime"] == "low"]
    return peak_hours, trough_hours


def minutes_to_next_event(current_hour: int, target_hours: list[int]) -> Optional[int]:
    """Minutes until the next hour from target_hours, relative to current_hour."""
    if not target_hours:
        return None
    for offset in range(1, 25):
        candidate = (current_hour + offset) % 24
        if candidate in target_hours:
            return offset * 60
    return None
