# serve/feature_eng.py — 14-channel window → 39-feature sklearn vector
import numpy as np

_CHAN_MAP = {
    "pfcp_sessions_total":     "rate_pfcp_sessions",
    "port_bytes_N3_rx_rate":   "rate_bytes_N3_rx",
    "port_bytes_N6_tx_rate":   "rate_bytes_N6_tx",
    "port_pkts_N3_rx_rate":    "rate_bytes_N3_tx",   # ponytail: packet rate as byte-rate proxy
    "port_pkts_N6_tx_rate":    "rate_bytes_N6_rx",   # ponytail: packet rate as byte-rate proxy
    "port_dropped_N3_rx_rate": "rate_drops_N3",
    "port_dropped_N6_rx_rate": "rate_drops_N6",
}

_ROLLING = [
    "rate_pfcp_sessions", "rate_bytes_N3_rx", "rate_bytes_N3_tx",
    "rate_bytes_N6_rx", "rate_bytes_N6_tx", "rate_drops_N3", "rate_drops_N6",
]

SKLEARN_FEATURE_NAMES: list[str] = [
    "rate_pfcp_sessions", "rate_bytes_N3_rx", "rate_bytes_N3_tx",
    "rate_bytes_N6_rx", "rate_bytes_N6_tx", "rate_drops_N3", "rate_drops_N6",
    "rx_tx_ratio_N3", "rx_tx_ratio_N6", "drop_fraction_N3", "drop_fraction_N6",
    *(f"{ch}_{s}_5m" for ch in _ROLLING for s in ("mean", "std", "min", "max")),
]
N_FEATURES: int = len(SKLEARN_FEATURE_NAMES)  # 39 — matches models/scaler_params.json


def extract_features(channels: list[list[float]], channel_names: list[str]) -> list[float]:
    """Map a (C, 512) window to the 39-feature vector the sklearn scaler expects."""
    base: dict[str, np.ndarray] = {}
    for name, row in zip(channel_names, channels):
        mapped = _CHAN_MAP.get(name)
        if mapped:
            base[mapped] = np.asarray(row, dtype=float)

    def _last(key: str) -> float:
        arr = base.get(key)
        return float(arr[-1]) if arr is not None and len(arr) else 0.0

    r_pfcp  = _last("rate_pfcp_sessions")
    r_n3_rx = _last("rate_bytes_N3_rx")
    r_n3_tx = _last("rate_bytes_N3_tx")
    r_n6_rx = _last("rate_bytes_N6_rx")
    r_n6_tx = _last("rate_bytes_N6_tx")
    r_dn3   = _last("rate_drops_N3")
    r_dn6   = _last("rate_drops_N6")

    feats: list[float] = [
        r_pfcp, r_n3_rx, r_n3_tx, r_n6_rx, r_n6_tx, r_dn3, r_dn6,
        r_n3_rx / (r_n6_tx + 1e-9),
        r_n3_tx / (r_n6_rx + 1e-9),
        r_dn3   / (r_n3_rx + 1e-9),
        r_dn6   / (r_n6_tx + 1e-9),
    ]

    for ch in _ROLLING:
        arr = base.get(ch)
        w = arr[-300:] if arr is not None and len(arr) else np.zeros(1)
        feats += [float(np.mean(w)), float(np.std(w)), float(np.min(w)), float(np.max(w))]

    return [0.0 if not np.isfinite(v) else v for v in feats]


def channel_zscores(channels: list[list[float]], channel_names: list[str]) -> dict[str, float]:
    """Per-channel |z-score| of the latest sample vs its own 5m rolling mean/std.

    Reuses the same rolling window `extract_features` already computes, so this
    is a pragmatic attribution proxy (not true SHAP) for which raw channel(s)
    look most deviant whenever any tier (IF/RF/MOMENT) flags an anomaly —
    those models only ever produce a single aggregate score, not a per-channel
    breakdown.
    """
    scores: dict[str, float] = {}
    for name, row in zip(channel_names, channels):
        mapped = _CHAN_MAP.get(name)
        if not mapped:
            continue
        arr = np.asarray(row, dtype=float)
        if arr.size == 0:
            continue
        w = arr[-300:]
        mean, std = float(np.mean(w)), float(np.std(w))
        last = float(arr[-1])
        z = abs(last - mean) / (std + 1e-9)
        scores[name] = z if np.isfinite(z) else 0.0
    return scores
