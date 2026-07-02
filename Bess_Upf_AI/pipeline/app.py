# pipeline/app.py
import json
import logging
import os

import requests
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


def _call_detect(keyed: tuple[str, tuple[dict, list]]) -> dict | None:
    upf_id, (msg, channels_array) = keyed
    try:
        resp = _session.post(
            f"{RAY_SERVE_URL}/detect",
            json={"channels": channels_array, "channel_names": ML_CHANNELS},
            timeout=5.0,
        )
        resp.raise_for_status()
        result: dict = resp.json()
    except Exception as exc:
        log.error("detect call failed for %s: %s", upf_id, exc, exc_info=True)
        result = {"anomaly": False, "anomaly_score": 0.0}

    result["upf_id"] = upf_id
    result["ts"] = msg.get("ts")
    return result if result.get("anomaly") else None


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

detections = op.map("detect", windowed, _call_detect)

# filter_map drops None (_call_detect returns None when anomaly=False)
anomalies = op.filter_map("anomaly-only", detections, lambda x: x)

anomaly_msgs = op.map("to-sink-msg", anomalies, _to_sink_msg)

op.output(
    "kafka-out",
    anomaly_msgs,
    KafkaSink(brokers=[KAFKA_BROKER], topic="upf.anomalies.critical"),
)
