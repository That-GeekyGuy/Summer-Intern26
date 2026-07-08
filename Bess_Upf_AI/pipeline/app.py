# pipeline/app.py
import json
import logging
import os
from datetime import timedelta

import requests
import numpy as np
import bytewax.operators as op
from bytewax.connectors.kafka import KafkaSource, KafkaSink, KafkaSinkMessage
from bytewax.dataflow import Dataflow

from pipeline.window import WindowState, ML_CHANNELS

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

KAFKA_BROKER = os.getenv("KAFKA_BROKER", "redpanda:9092")
RAY_SERVE_URL = os.getenv("RAY_SERVE_URL", "http://serve:8000")

_session = requests.Session()


def _parse(raw_msg) -> dict:
    return json.loads(raw_msg.value)


def _window_mapper(
    state: WindowState | None, msg: dict
) -> tuple[WindowState, tuple[dict, list] | None]:
    if state is None:
        state = WindowState()
    state.update(msg)
    payload = (msg, state.to_array()) if state.is_full() else None
    return state, payload


def _call_detect_batch(item: tuple[str, list[tuple[dict, list]]]) -> list[dict]:
    upf_id, entries = item
    reqs = []
    stat_anomalies = []

    for msg, channels_array in entries:
        stat_anomaly = False
        stat_score = 0.0
        stat_channels = []
        try:
            drop_idx = ML_CHANNELS.index("port_dropped_N3_rx_rate")
            drop_window = channels_array[drop_idx]
            current_drop = msg.get("port_dropped_N3_rx_rate", 0.0)
            
            arr = np.array(drop_window)
            mean = np.mean(arr)
            std = np.std(arr)
            if std > 0:
                z_score = float((current_drop - mean) / std)
                if z_score > 2.5 and current_drop > mean + std:
                    stat_anomaly = True
                    stat_score = max(stat_score, z_score)
                    stat_channels.append("port_dropped_N3_rx_rate")
        except (ValueError, IndexError):
            pass

        try:
            sess_idx = ML_CHANNELS.index("pfcp_sessions_total")
            sess_window = channels_array[sess_idx]
            current_sess = msg.get("pfcp_sessions_total", 0.0)
            arr2 = np.array(sess_window)
            mean2 = np.mean(arr2)
            std2 = np.std(arr2)
            if std2 > 0:
                z2 = float((current_sess - mean2) / std2)
                if abs(z2) > 3.0:
                    stat_anomaly = True
                    stat_score = max(stat_score, abs(z2))
                    stat_channels.append("pfcp_sessions_total")
        except (ValueError, IndexError):
            pass

        stat_anomalies.append((stat_anomaly, stat_score, stat_channels))
        reqs.append({
            "channels": channels_array, 
            "channel_names": ML_CHANNELS,
            "upf_id": upf_id, 
            "ts": msg.get("ts", 0.0)
        })

    if not reqs:
        return []

    results = []
    try:
        resp = _session.post(
            f"{RAY_SERVE_URL}/detect_batch",
            json=reqs,
            timeout=5.0,
        )
        resp.raise_for_status()
        ml_results = resp.json()
    except Exception as exc:
        log.error("detect_batch call failed: %s", exc)
        ml_results = [{"anomaly": False, "anomaly_score": 0.0} for _ in reqs]

    for (msg, _), (stat_anomaly, stat_score, stat_channels), ml_res in zip(entries, stat_anomalies, ml_results):
        if stat_anomaly:
            ml_res["anomaly"] = True
            ml_res["anomaly_score"] = max(ml_res.get("anomaly_score", 0.0), stat_score)
            ml_res["model_version"] = ml_res.get("model_version", "statistical-zscore-v1")
            
            # extend instead of override
            existing_channels = ml_res.get("top_anomalous_channels", [])
            ml_res["top_anomalous_channels"] = list(set(existing_channels + stat_channels))

        ml_res["upf_id"] = upf_id
        ml_res["ts"] = msg.get("ts", 0.0)
        ml_res.setdefault("threshold", 2.5)
        ml_res.setdefault("window_end_offset", 0)
        ml_res.setdefault("top_anomalous_channels", [])
        ml_res.setdefault("model_version", "unknown")

        if ml_res.get("anomaly"):
            results.append(ml_res)

    return results


def _to_sink_msg(result: dict) -> KafkaSinkMessage:
    return KafkaSinkMessage(
        key=result["upf_id"].encode(),
        value=json.dumps(result, allow_nan=False).encode(),
    )


flow = Dataflow("upf-pipeline")

raw = op.input(
    "kafka-in",
    flow,
    KafkaSource(brokers=[KAFKA_BROKER], topics=["upf.metrics.raw"]),
)

parsed = op.map("parse", raw, _parse)
keyed = op.key_on("key-by-upf", parsed, lambda m: m["upf_id"])

# stateful_map suppresses None outputs — window not yet full
windowed = op.stateful_map("window", keyed, _window_mapper)

# filter out None payloads (window not yet full)
windowed_full = op.filter("window-full", windowed, lambda k_v: k_v[1] is not None)

# Batch items up to 50 or 1 second, whichever comes first
batched = op.collect("batch", windowed_full, max_size=50, timeout=timedelta(seconds=1))

anomalies = op.flat_map("detect-batch", batched, _call_detect_batch)

anomaly_msgs = op.map("to-sink-msg", anomalies, _to_sink_msg)

op.output(
    "kafka-out",
    anomaly_msgs,
    KafkaSink(brokers=[KAFKA_BROKER], topic="upf.anomalies.critical"),
)
