"""
Dynamic metric discovery and fetching from Prometheus.

Instead of hardcoding metric names, this module:
  1. Calls /api/v1/metadata to discover every upf_* and pfcp_* metric and its type.
  2. Calls /api/v1/query_range with just the metric name (no label filters) to fetch
     every label combination that exists at that moment.
  3. Applies the correct transformation per type:
       counter   → .diff() to get per-step delta (avoids ever-increasing raw values)
       gauge     → use as-is
       histogram → skip _bucket series; treat _count/_sum as counters
       summary   → quantile labels are gauges; _count/_sum are counters
  4. Flattens each (metric_name, label_set) pair into a uniquely named DataFrame column.

When the UPF gains new metrics (e.g. after a BESS upgrade or config change),
they are picked up automatically on the next run.
"""

import requests
import pandas as pd
from datetime import datetime

# Only fetch metrics under these prefixes — ignores Go runtime / process metrics.
METRIC_PREFIXES = ("upf_", "pfcp_")

# Metrics whose individual label cardinality is too high for direct ML features
# (e.g. one row per UE session). These are aggregated with sum() instead of
# creating one column per label combination.
HIGH_CARDINALITY_PREFIXES = ("upf_session_",)

# Maximum distinct label-set columns before a metric is force-aggregated.
CARDINALITY_LIMIT = 20

# Prometheus internal labels — not useful as ML features and bloat column names.
EXCLUDED_LABELS = {"__name__", "instance", "job"}


# ── Helpers ────────────────────────────────────────────────────────────────

def _col_name(metric: str, labels: dict) -> str:
    """Build a flat column name from a metric name and its label dict.

    Example:
      metric="upf_packets_count", labels={"iface":"Access","dir":"rx"}
      → "upf_packets_count__dir_rx__iface_Access"
    """
    if not labels:
        return metric
    parts = "__".join(f"{k}_{v}" for k, v in sorted(labels.items()))
    return f"{metric}__{parts}"


def _is_counter_type(metric_name: str, metric_type: str) -> bool:
    """Return True if this metric's values should be diff'd before use."""
    if metric_type == "counter":
        return True
    # Histogram _count and _sum are always counters even though the parent
    # metric is typed 'histogram'.
    if metric_type in ("histogram", "summary"):
        return metric_name.endswith(("_count", "_sum"))
    return False


# ── Discovery ──────────────────────────────────────────────────────────────

def discover_metrics(prom_url: str) -> dict[str, str]:
    """Return {metric_name: type_string} for all UPF/PFCP metrics.

    Calls Prometheus /api/v1/metadata.  Type is one of:
      "counter", "gauge", "histogram", "summary", "untyped"

    Histogram metrics generate three sub-metrics in Prometheus:
      <name>_bucket, <name>_count, <name>_sum
    We skip _bucket here because bucket data is not useful for tabular ML.
    """
    resp = requests.get(f"{prom_url}/api/v1/metadata", timeout=10)
    resp.raise_for_status()
    raw: dict = resp.json().get("data", {})

    metrics: dict[str, str] = {}
    for name, meta_list in raw.items():
        if not any(name.startswith(p) for p in METRIC_PREFIXES):
            continue
        mtype = meta_list[0].get("type", "untyped") if meta_list else "untyped"

        if mtype == "histogram":
            # The base histogram name has no direct time series in Prometheus —
            # only _bucket, _count, _sum exist. Skip _bucket (not useful for ML)
            # and register _count/_sum explicitly as counters.
            metrics[name + "_count"] = "counter"
            metrics[name + "_sum"]   = "counter"

        elif mtype == "summary":
            # The quantile series (upf_jitter_ns{quantile="50"} etc.) use the
            # base name — keep those.
            metrics[name] = mtype
            # _count and _sum are separate time series not listed in metadata
            # but they carry the most useful signal (packet/message rate).
            metrics[name + "_count"] = "counter"
            metrics[name + "_sum"]   = "counter"

        else:
            metrics[name] = mtype

    return metrics


# ── Fetching ───────────────────────────────────────────────────────────────

def _fetch_raw(prom_url: str, query: str, start: datetime, end: datetime, step: str) -> list:
    """Run one query_range call and return the raw result list."""
    resp = requests.get(
        f"{prom_url}/api/v1/query_range",
        params={
            "query": query,
            "start": start.timestamp(),
            "end":   end.timestamp(),
            "step":  step,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["data"]["result"]


def _series_to_df(values: list) -> pd.Series:
    """Convert a Prometheus [[timestamp, value], ...] list to a float Series."""
    df = pd.DataFrame(values, columns=["ts", "value"])
    df["ts"]    = pd.to_datetime(df["ts"], unit="s", utc=True)
    df["value"] = df["value"].astype(float)
    return df.set_index("ts")["value"]


def _fetch_metric(
    prom_url: str,
    name: str,
    mtype: str,
    start: datetime,
    end: datetime,
    step: str,
) -> dict[str, pd.Series]:
    """
    Fetch all label combinations for one metric.
    Returns {column_name: pd.Series}.

    High-cardinality metrics (e.g. per-UE-session) are aggregated with sum()
    into a single column to avoid a column-explosion in the output DataFrame.
    """
    results = _fetch_raw(prom_url, name, start, end, step)
    if not results:
        return {}

    is_hc = any(name.startswith(p) for p in HIGH_CARDINALITY_PREFIXES)
    force_aggregate = is_hc or len(results) > CARDINALITY_LIMIT

    should_diff = _is_counter_type(name, mtype)

    if force_aggregate:
        # Sum all label combinations into one series.
        combined = sum(_series_to_df(r["values"]) for r in results)
        if should_diff:
            combined = combined.diff()
        return {name: combined}

    # Each distinct label set becomes its own column.
    columns: dict[str, pd.Series] = {}
    for result in results:
        labels = {k: v for k, v in result["metric"].items() if k not in EXCLUDED_LABELS}
        col  = _col_name(name, labels)
        s    = _series_to_df(result["values"])
        if should_diff:
            s = s.diff()
        columns[col] = s

    return columns


# ── Public API ─────────────────────────────────────────────────────────────

def fetch_all(
    prom_url: str,
    start: datetime,
    end: datetime,
    step: str = "30s",
) -> pd.DataFrame:
    """
    Discover every UPF/PFCP metric in Prometheus, fetch it, and return a
    single wide DataFrame (rows = timestamps, columns = metric label combos).

    New metrics added to the UPF (config changes, BESS upgrades) are
    picked up automatically — no code changes required.
    """
    metrics = discover_metrics(prom_url)
    if not metrics:
        raise RuntimeError(
            f"No upf_*/pfcp_* metrics found at {prom_url}. "
            "Is the UPF running and Prometheus scraping :8080/metrics?"
        )

    print(f"Discovered {len(metrics)} metrics: {sorted(metrics)}")

    all_series: dict[str, pd.Series] = {}
    for name, mtype in sorted(metrics.items()):
        series_map = _fetch_metric(prom_url, name, mtype, start, end, step)
        all_series.update(series_map)

    df = pd.DataFrame(all_series)
    df.sort_index(inplace=True)
    df.dropna(how="all", inplace=True)

    # ── Derived features ────────────────────────────────────────────────────
    # Drop ratio: packets dropped ÷ packets received, per interface/direction.
    # A spike here is the strongest single anomaly signal for ML.
    for iface in ("Access", "Core"):
        for direction in ("rx", "tx"):
            dropped_col = f"upf_dropped_count__dir_{direction}__iface_{iface}"
            packets_col = f"upf_packets_count__dir_{direction}__iface_{iface}"
            if dropped_col in df and packets_col in df:
                safe_packets = df[packets_col].replace(0, float("nan"))
                df[f"drop_ratio__{iface}_{direction}"] = (
                    df[dropped_col] / safe_packets
                )

    return df
