# pipeline/forecast_check.py — Tier 3 predictive (forecast-breach) anomalies.
#
# Runs as an independent background process alongside the bytewax dataflow
# (see start.sh, same pattern as scrape_to_kafka.py) rather than inside the
# dataflow graph — bytewax's Dataflow is message-driven off the Kafka source,
# with no straightforward periodic/timer input, so a plain polling loop with
# its own Kafka producer is the simpler, lower-risk fit here.
#
# Every CHECK_INTERVAL_SECS, for each target in FORECAST_TARGETS (mirrors
# frontend/src/features/forecast/ForecastPage.tsx's FORECAST_METRICS and
# config/detection/rules.yml's forecast.targets — port_dropped_count is
# accel-only there, capacity: null, so it has no breach target here), fetch
# recent context from ClickHouse, ask the chronos sidecar for a P10/P50/P90
# projection, and if the P90 (upper) band crosses capacity within the
# forecast horizon, publish a predictive anomaly_events row.
import json
import logging
import os
import time
from datetime import datetime

import requests
from confluent_kafka import Producer

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

KAFKA_BROKER = os.getenv("KAFKA_BROKER", "redpanda:9092")
CLICKHOUSE_DSN = os.getenv("CLICKHOUSE_DSN", "http://clickhouse:8123")
CHRONOS_URL = os.getenv("CHRONOS_URL", "http://chronos:8084")
CHECK_INTERVAL_SECS = float(os.getenv("FORECAST_CHECK_INTERVAL_SECS", "60"))

# chronos/app.py's HORIZON_STEPS only supports "short" (10 steps) or "long"
# (30 steps); at STEP_SECONDS=60 that's a 30-minute max lookahead, not the
# full 1h config/detection/rules.yml documents as the target horizon.
CHRONOS_HORIZON = "long"
FORECAST_HORIZON_LABEL = "30m"
HORIZON_SECONDS = 30 * 60
DEDUP_SECONDS = HORIZON_SECONDS // 2  # per rules.yml: suppress re-fire for horizon/2

FORECAST_TARGETS = [
    {"metric": "pfcp_sessions_total", "capacity": 50_000},
    {"metric": "pfcp_sessions_total_cluster", "capacity": 65_000},
    {"metric": "port_bytes_count", "capacity": 75_000_000},
]

# Same bucketed-query shapes as analysis/internal/chclient/client.go's
# channelBucketQueries, kept in sync deliberately.
_CONTEXT_QUERIES = {
    "pfcp_sessions_total": """
        SELECT bucket, sum(v) AS value FROM (
            SELECT upf_id, toStartOfInterval(ts, INTERVAL {step} SECOND) AS bucket,
                   argMax(pfcp_sessions_total, ts) AS v
            FROM bess_upf.upf_metrics
            WHERE ts >= now() - INTERVAL {lookback} SECOND
            GROUP BY upf_id, bucket
        ) GROUP BY bucket ORDER BY bucket
    """,
    "pfcp_sessions_total_cluster": """
        SELECT bucket, sum(v) AS value FROM (
            SELECT upf_id, toStartOfInterval(ts, INTERVAL {step} SECOND) AS bucket,
                   argMax(pfcp_sessions_total, ts) AS v
            FROM bess_upf.upf_metrics
            WHERE ts >= now() - INTERVAL {lookback} SECOND
            GROUP BY upf_id, bucket
        ) GROUP BY bucket ORDER BY bucket
    """,
    "port_bytes_count": """
        SELECT bucket, sum(v) AS value FROM (
            SELECT upf_id, toStartOfInterval(ts, INTERVAL {step} SECOND) AS bucket,
                   argMax(port_bytes_N3_rx_rate, ts) AS v
            FROM bess_upf.upf_metrics
            WHERE ts >= now() - INTERVAL {lookback} SECOND
            GROUP BY upf_id, bucket
        ) GROUP BY bucket ORDER BY bucket
    """,
}

_last_fired: dict[str, float] = {}


def _clickhouse_user_pass() -> tuple[str | None, str | None]:
    # CLICKHOUSE_DSN is e.g. http://chuser:PASSWORD@clickhouse-0...:8123/bess_upf
    from urllib.parse import urlsplit
    u = urlsplit(CLICKHOUSE_DSN)
    return u.username, u.password


def _ch_query_json(sql: str) -> list[dict]:
    from urllib.parse import urlsplit, quote

    u = urlsplit(CLICKHOUSE_DSN)
    url = f"{u.scheme}://{u.hostname}:{u.port}/?query={quote(sql + ' FORMAT JSON')}"
    if u.path and u.path != "/":
        url += "&database=" + quote(u.path.lstrip("/"))

    user, password = _clickhouse_user_pass()
    headers = {}
    if user:
        headers["X-ClickHouse-User"] = user
        headers["X-ClickHouse-Key"] = password or ""

    resp = requests.get(url, headers=headers, timeout=10.0)
    resp.raise_for_status()
    return resp.json().get("data", [])


def _fetch_context(metric: str, lookback_s: int = 3600, step_s: int = 60) -> list[float]:
    tmpl = _CONTEXT_QUERIES.get(metric)
    if tmpl is None:
        return []
    sql = tmpl.format(step=step_s, lookback=lookback_s)
    try:
        rows = _ch_query_json(sql)
    except Exception as exc:
        log.warning("ClickHouse context query failed for %s: %s", metric, exc)
        return []

    vals: list[float] = []
    for row in rows:
        try:
            vals.append(float(row["value"]))
        except (TypeError, ValueError, KeyError):
            continue
    return vals


def _fetch_forecast(metric: str, context: list[float]) -> dict | None:
    try:
        resp = requests.post(
            f"{CHRONOS_URL}/intervals",
            json={"channel": metric, "context": context, "horizon": CHRONOS_HORIZON},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.warning("chronos forecast failed for %s: %s", metric, exc)
        return None
    return data if data.get("available") else None


def _check_target(target: dict) -> dict | None:
    metric = target["metric"]
    capacity = target["capacity"]

    context = _fetch_context(metric)
    if len(context) < 5:
        return None

    forecast = _fetch_forecast(metric, context)
    if forecast is None:
        return None

    p90 = forecast.get("p90") or []
    timestamps = forecast.get("timestamps") or []

    # Earliest point where the upper (p90) band crosses capacity — gives
    # operators the most possible warning rather than waiting for the median.
    for value, ts_str in zip(p90, timestamps):
        if value < capacity:
            continue
        try:
            crossing_ts = datetime.fromisoformat(ts_str).timestamp()
        except ValueError:
            continue
        return {
            "upf_id": "cluster",
            "ts": time.time(),
            "anomaly": True,
            "anomaly_score": float(value),
            "threshold": float(capacity),
            "top_anomalous_channels": [metric],
            "model_version": "chronos-forecast-v1",
            "window_end_offset": 0,
            "predicted_crossing_time": crossing_ts,
            "forecast_horizon": FORECAST_HORIZON_LABEL,
        }
    return None


def main() -> None:
    producer = Producer({"bootstrap.servers": KAFKA_BROKER})
    log.info(
        "Forecast-check started: targets=%s interval=%ss",
        [t["metric"] for t in FORECAST_TARGETS], CHECK_INTERVAL_SECS,
    )

    while True:
        tick_start = time.time()
        for target in FORECAST_TARGETS:
            metric = target["metric"]
            try:
                event = _check_target(target)
            except Exception as exc:
                log.warning("forecast check failed for %s: %s", metric, exc)
                continue
            if event is None:
                continue

            last = _last_fired.get(metric, 0.0)
            if tick_start - last < DEDUP_SECONDS:
                continue  # already warned recently — suppress re-fire

            try:
                payload = json.dumps(event, allow_nan=False).encode()
            except ValueError:
                log.warning("skipping forecast event for %s — non-finite values", metric)
                continue

            producer.produce(
                "upf.anomalies.critical", key=event["upf_id"].encode(), value=payload,
            )
            producer.flush()
            _last_fired[metric] = tick_start
            log.info(
                "predictive breach warning: %s crossing=%s capacity=%s",
                metric, event["predicted_crossing_time"], target["capacity"],
            )

        elapsed = time.time() - tick_start
        time.sleep(max(0.0, CHECK_INTERVAL_SECS - elapsed))


if __name__ == "__main__":
    main()
