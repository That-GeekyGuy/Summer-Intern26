# pipeline/scrape_to_kafka.py
import json
import logging
import os
import time

import requests
from confluent_kafka import Producer
from prometheus_client.parser import text_string_to_metric_families

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

KAFKA_BROKER = os.getenv("KAFKA_BROKER", "redpanda:9092")
UPF_SIM_METRICS = os.getenv("UPF_SIM_METRICS", "http://upf-sim:8090/metrics")
SCRAPE_INTERVAL = float(os.getenv("SCRAPE_INTERVAL_SECS", "1.0"))

_PORT_FAMILIES: dict[str, str] = {
    "port_bytes_count": "port_bytes",
    "port_packets_count": "port_pkts",
    "port_dropped_count": "port_dropped",
}

_COUNTER_PREFIXES = (
    "port_bytes_",
    "port_pkts_",
    "port_dropped_",
    "process_cpu_seconds_total",
)


def flatten_key(sample_name: str, labels: dict) -> tuple[str | None, str | None]:
    """
    Map a Prometheus sample name + label dict to a flat snake_case key.
    Returns (metric_key, upf_id). Returns (None, None) to skip this sample.
    node_id label is extracted as upf_id and not included in the key.
    """
    # Skip histogram/summary internals (not from port_* families)
    for suffix in ("_bucket", "_sum", "_count"):
        if sample_name.endswith(suffix) and not any(
            sample_name.startswith(p) for p in _PORT_FAMILIES
        ):
            return None, None

    upf_id: str | None = labels.get("node_id")
    clean = {k: v for k, v in labels.items() if k != "node_id"}

    for full_prefix, short in _PORT_FAMILIES.items():
        if sample_name.startswith(full_prefix):
            iface = clean.get("iface", "")
            dir_ = clean.get("dir", "")
            return f"{short}_{iface}_{dir_}", upf_id

    if sample_name.startswith("upf_sim_scenario"):
        return f"upf_sim_scenario_{clean.get('mode', 'unknown')}", upf_id

    if not clean:
        return sample_name, upf_id

    suffix_val = "_".join(v for _, v in sorted(clean.items()))
    return f"{sample_name}_{suffix_val}", upf_id


def parse_prometheus_text(text: str) -> tuple[dict[str, float], str | None]:
    """
    Parse Prometheus exposition text into a flat dict.
    Returns (metrics_dict, upf_id).
    Does NOT compute rates.
    """
    result: dict[str, float] = {}
    upf_id: str | None = None

    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            key, uid = flatten_key(sample.name, sample.labels)
            if key is None:
                continue
            if uid is not None:
                upf_id = uid
            result[key] = sample.value

    return result, upf_id


def compute_rate(
    key: str,
    current_value: float,
    current_ts: float,
    prev: dict[str, tuple[float, float]],
) -> float | None:
    """
    Compute per-second rate for a counter metric.
    Mutates prev in place. Returns None on first tick or counter reset.
    Never returns 0.0 as a fallback for missing data.
    """
    if key not in prev:
        prev[key] = (current_value, current_ts)
        return None

    prev_value, prev_ts = prev[key]
    elapsed = current_ts - prev_ts

    if elapsed <= 0 or current_value < prev_value:
        prev[key] = (current_value, current_ts)
        log.info("Counter reset or zero elapsed for %s — skipping rate", key)
        return None

    rate = (current_value - prev_value) / elapsed
    prev[key] = (current_value, current_ts)
    return rate


def build_message(
    metrics: dict[str, float],
    upf_id: str,
    ts: float,
    prev_state: dict[str, tuple[float, float]],
) -> dict:
    msg: dict = {"upf_id": upf_id, "ts": ts}
    for key, value in metrics.items():
        if key == "process_cpu_seconds_total":
            msg[key] = float(value)
        else:
            msg[key] = int(value)

    for key, value in metrics.items():
        if any(key.startswith(p) for p in _COUNTER_PREFIXES):
            rate = compute_rate(key, value, ts, prev_state)
            if rate is not None:
                if key == "process_cpu_seconds_total":
                    rate_key = "process_cpu_rate"
                else:
                    rate_key = key + "_rate"
                msg[rate_key] = rate

    return msg


def main() -> None:
    producer = Producer({"bootstrap.servers": KAFKA_BROKER})
    log.info("Scraper started: %s -> upf.metrics.raw", UPF_SIM_METRICS)

    prev_state: dict[str, tuple[float, float]] = {}

    try:
        while True:
            tick_start = time.time()
            try:
                resp = requests.get(UPF_SIM_METRICS, timeout=15.0)
                resp.raise_for_status()

                metrics, upf_id = parse_prometheus_text(resp.text)
                if upf_id is None:
                    log.warning("No node_id label in metrics — skipping tick")
                else:
                    msg = build_message(metrics, upf_id, tick_start, prev_state)
                    try:
                        payload = json.dumps(msg, allow_nan=False).encode()
                    except ValueError:
                        log.warning("Skipping tick for %s — metrics contain NaN/Inf", upf_id)
                        continue
                    producer.produce(
                        "upf.metrics.raw",
                        key=upf_id.encode(),
                        value=payload,
                    )
                    producer.flush()

            except requests.RequestException as exc:
                log.warning("Scrape failed: %s — backing off 15s", exc)
                time.sleep(15.0)
                continue

            elapsed = time.time() - tick_start
            time.sleep(max(0.0, SCRAPE_INTERVAL - elapsed))
    finally:
        producer.flush()


if __name__ == "__main__":
    main()
