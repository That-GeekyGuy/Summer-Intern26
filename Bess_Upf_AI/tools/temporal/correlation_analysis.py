#!/usr/bin/env python3
"""
Metric correlation analysis — offline script.

Discovers which metrics move together and with what lag.
Run after prepare_dataset.py to build the correlation graph.

Outputs:
  models/correlation_graph.json  — lagged correlation edges (|r| > 0.6 only)
  tools/temporal/data/correlation_report.md — human-readable interpretation

Usage:
  python tools/temporal/correlation_analysis.py
  python tools/temporal/correlation_analysis.py --csv prometheus_full_export_20260622_115327.csv

Only edges with |correlation| > CORRELATION_THRESHOLD are included.
The graph is loaded by Brain 2 at startup for causal RCA language.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

REPO_ROOT  = Path(__file__).parent.parent.parent
MODELS_DIR = REPO_ROOT / "models"
DATA_DIR   = Path(__file__).parent / "data"

# Correlation edge threshold — below this, the relationship isn't reliable
CORRELATION_THRESHOLD = 0.6

# Max lag in 15s steps (20 steps = 5 minutes at 15s cadence)
MAX_LAG_STEPS = 20

# Channel pairs to evaluate — traffic channels only (Go runtime excluded)
TRAFFIC_CHANNELS = {
    "port_bytes_N3_rx_rate":    "port_bytes_count{dir=rx_iface=N3",
    "port_bytes_N6_tx_rate":    "port_bytes_count{dir=tx_iface=N6",
    "port_pkts_N3_rx_rate":     "port_packets_count{dir=rx_iface=N3",
    "port_dropped_N3_rx_rate":  "port_dropped_count{dir=rx_iface=N3",
    "port_dropped_N6_rx_rate":  "port_dropped_count{dir=rx_iface=N6",
    "pfcp_sessions_total":      "pfcp_sessions_total{instance=192.168.237.186:30093",
    "uoi_session_component":    "uoi_session_component",
    "uoi_throughput_component": "uoi_throughput_component",
    "drop_rate_percentage":     "drop_rate_percentage",
    "tsi_value":                "tsi_value{",
}

CHANNEL_DISPLAY = {
    "port_bytes_N3_rx_rate":    "N3 Inbound Throughput",
    "port_bytes_N6_tx_rate":    "N6 Outbound Throughput",
    "port_pkts_N3_rx_rate":     "N3 Packet Rate",
    "port_dropped_N3_rx_rate":  "N3 Drop Rate",
    "port_dropped_N6_rx_rate":  "N6 Drop Rate",
    "pfcp_sessions_total":      "PFCP Active Sessions",
    "uoi_session_component":    "UOI Session Component",
    "uoi_throughput_component": "UOI Throughput Component",
    "drop_rate_percentage":     "Drop Rate (%)",
    "tsi_value":                "Traffic Spike Index",
}


def load_csv(csv_path: Path) -> pd.DataFrame:
    log.info("loading %s", csv_path)
    df = pd.read_csv(str(csv_path), low_memory=False, on_bad_lines="skip")
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df.sort_values("timestamp").set_index("timestamp")


def find_col(df: pd.DataFrame, substring: str) -> str | None:
    for col in df.columns:
        if substring in col:
            return col
    return None


def is_counter(col: str) -> bool:
    for sub in ("port_bytes_count", "port_packets_count", "port_dropped_count",
                "pfcp_messages_total", "pfcp_sessions_total"):
        if sub in col:
            return True
    return False


def extract_channels(df: pd.DataFrame) -> pd.DataFrame:
    """Extract and rate-convert traffic channels. Returns a 15s-resampled DataFrame."""
    channels = {}
    for logical, substring in TRAFFIC_CHANNELS.items():
        col = find_col(df, substring)
        if col is None:
            log.warning("channel '%s' not found (substring: '%s') — skipping", logical, substring)
            continue
        series = df[col].resample("15s").last()
        if is_counter(col):
            series = series.diff().clip(lower=0) / 15.0
        else:
            series = df[col].resample("15s").mean()
        channels[logical] = series

    result = pd.DataFrame(channels)
    result = result.interpolate(method="linear", limit=10).dropna(how="all")
    log.info("extracted %d channels × %d rows", len(result.columns), len(result))
    return result


def compute_lagged_correlation(
    a: np.ndarray, b: np.ndarray, max_lag: int
) -> tuple[int, float]:
    """
    Find the lag (in steps) at which b most strongly follows a, within ±max_lag steps.
    Positive lag means b lags behind a (a precedes b).

    Returns (best_lag_steps, correlation_at_best_lag).
    """
    from scipy.signal import correlate

    a_centered = a - a.mean()
    b_centered = b - b.mean()

    corr = correlate(b_centered, a_centered, mode="full")
    lags = np.arange(-len(a) + 1, len(a))

    # Window to ±max_lag
    mid = len(a) - 1
    start = mid - max_lag
    end = mid + max_lag + 1
    windowed_corr = corr[start:end]
    windowed_lags = lags[start:end]

    best_idx = int(np.argmax(np.abs(windowed_corr)))
    best_lag = int(windowed_lags[best_idx])

    # Normalize
    denom = len(a) * (a.std() + 1e-9) * (b.std() + 1e-9)
    best_corr = float(windowed_corr[best_idx] / denom)

    return best_lag, best_corr


def interpret_edge(from_ch: str, to_ch: str, lag_steps: int, corr: float) -> str:
    lag_sec = lag_steps * 15
    from_name = CHANNEL_DISPLAY.get(from_ch, from_ch)
    to_name = CHANNEL_DISPLAY.get(to_ch, to_ch)

    direction = "positive" if corr > 0 else "inverse"
    if lag_steps > 0:
        return (
            f"{from_name} increases precede {to_name} changes by ~{lag_sec}s "
            f"({direction} correlation: {corr:.2f})"
        )
    elif lag_steps < 0:
        return (
            f"{to_name} changes precede {from_name} increases by ~{abs(lag_sec)}s "
            f"({direction} correlation: {corr:.2f})"
        )
    else:
        return f"{from_name} and {to_name} move together simultaneously ({direction}: {corr:.2f})"


def run(csv_path: Path):
    if not csv_path.exists():
        log.error("CSV not found: %s", csv_path)
        sys.exit(1)

    raw = load_csv(csv_path)
    channels_df = extract_channels(raw)

    channel_names = list(channels_df.columns)
    n = len(channel_names)
    coverage_hours = (channels_df.index.max() - channels_df.index.min()).total_seconds() / 3600
    coverage_days = coverage_hours / 24

    log.info("data coverage: %.2f days", coverage_days)

    edges = []
    pairs_evaluated = 0
    pairs_significant = 0

    for i in range(n):
        for j in range(i + 1, n):
            a_name = channel_names[i]
            b_name = channel_names[j]

            a = channels_df[a_name].dropna().values.astype(float)
            b = channels_df[b_name].dropna().values.astype(float)

            # Align on common length
            min_len = min(len(a), len(b))
            if min_len < MAX_LAG_STEPS * 4:
                continue

            a = a[-min_len:]
            b = b[-min_len:]

            try:
                best_lag, best_corr = compute_lagged_correlation(a, b, MAX_LAG_STEPS)
            except Exception as e:
                log.debug("correlation failed for %s×%s: %s", a_name, b_name, e)
                continue

            pairs_evaluated += 1

            if abs(best_corr) >= CORRELATION_THRESHOLD:
                pairs_significant += 1
                interpretation = interpret_edge(a_name, b_name, best_lag, best_corr)
                edges.append({
                    "from":           a_name,
                    "to":             b_name,
                    "lag_seconds":    best_lag * 15,
                    "correlation":    round(best_corr, 4),
                    "abs_correlation": round(abs(best_corr), 4),
                    "interpretation": interpretation,
                })
                log.info("  edge: %s → %s  lag=%ds  r=%.3f",
                         a_name, b_name, best_lag * 15, best_corr)

    # Sort by absolute correlation descending
    edges.sort(key=lambda e: -e["abs_correlation"])

    graph = {
        "generated_at":      datetime.now(timezone.utc).isoformat(),
        "data_coverage_days": round(coverage_days, 2),
        "pairs_evaluated":   pairs_evaluated,
        "pairs_significant": pairs_significant,
        "correlation_threshold": CORRELATION_THRESHOLD,
        "max_lag_seconds":   MAX_LAG_STEPS * 15,
        "edges":             edges,
        "warning": (
            "Correlation graph based on {:.1f} days of data. "
            "Lagged correlations are most reliable with ≥ 7 days of traffic."
        ).format(coverage_days) if coverage_days < 7 else None,
    }

    # Write correlation graph
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = MODELS_DIR / "correlation_graph.json"
    out_path.write_text(json.dumps(graph, indent=2))
    log.info("written %s  (%d edges)", out_path, len(edges))

    # Write markdown report
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    report_path = DATA_DIR / "correlation_analysis_report.md"
    _write_report(report_path, graph, coverage_days)
    log.info("written %s", report_path)

    return graph


def _write_report(path: Path, graph: dict, coverage_days: float):
    lines = [
        "# Metric Correlation Analysis Report",
        "",
        f"Generated: {graph['generated_at']}",
        f"Data coverage: {coverage_days:.2f} days",
        f"Correlation threshold: |r| ≥ {graph['correlation_threshold']}",
        f"Max lag window: ±{graph['max_lag_seconds']}s",
        "",
    ]

    if coverage_days < 3:
        lines += [
            "⚠ **WARNING: Thin data baseline.**",
            f"Only {coverage_days:.1f} days of data. Correlations may be spurious.",
            "Rerun after accumulating ≥ 7 days for reliable edges.",
            "",
        ]

    lines += [
        f"## Results: {graph['pairs_evaluated']} pairs evaluated, "
        f"{graph['pairs_significant']} significant edges",
        "",
    ]

    if not graph["edges"]:
        lines += ["No significant correlations found (|r| < threshold for all pairs).", ""]
    else:
        lines += ["## Significant Lagged Correlations", ""]
        for e in graph["edges"]:
            lines += [
                f"### {CHANNEL_DISPLAY.get(e['from'], e['from'])} → {CHANNEL_DISPLAY.get(e['to'], e['to'])}",
                "",
                f"- **Correlation**: {e['correlation']:.3f}",
                f"- **Lag**: {e['lag_seconds']}s ({e['lag_seconds'] // 15} steps at 15s cadence)",
                f"- **Interpretation**: {e['interpretation']}",
                "",
            ]

    lines += [
        "## Usage in Brain 2 RCA",
        "",
        "The correlation graph is loaded by Brain 2 at startup as RAG context.",
        "When anomalous channels match a graph edge, Brain 2 describes the causal",
        "relationship rather than listing metrics independently.",
        "",
        "Example: if N3 rx rate and N3 drop rate are both elevated, Brain 2 can say",
        "\"N3 rx spikes historically precede N3 drop spikes by ~30s — consistent",
        "with buffer saturation\" rather than \"two metrics are elevated\".",
        "",
        "## Future: External Signal Integration",
        "",
        "Geopolitical events, weather patterns, and major network outages can shift",
        "traffic baselines significantly. Integrating external signals into the",
        "temporal context is on the roadmap but requires ≥ 1 year of historical",
        "data to establish reliable correlations between external events and UPF",
        "traffic patterns. This feature is deferred until sufficient data accumulates.",
    ]

    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Metric correlation analysis")
    parser.add_argument("--csv", default=str(REPO_ROOT / "prometheus_full_export_20260622_115327.csv"))
    args = parser.parse_args()
    run(Path(args.csv))
