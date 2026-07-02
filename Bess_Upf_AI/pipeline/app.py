import os
import json
import logging
from collections import deque
import requests
from confluent_kafka import Consumer, Producer

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

KAFKA_BROKER = os.getenv("KAFKA_BROKER", "redpanda:9092")
RAY_SERVE_URL = os.getenv("RAY_SERVE_URL", "http://serve:8000")

# Flink/Bytewax equivalent: Sliding window state
window_size = 512
channels_state = {i: deque(maxlen=window_size) for i in range(14)}
channel_names = [
    "port_bytes_N3_rx_rate", "port_bytes_N6_tx_rate", "port_pkts_N3_rx_rate",
    "port_dropped_N3_rx_rate", "port_dropped_N6_rx_rate", "pfcp_sessions_total",
    "pfcp_session_setup_rate", "dl_throughput_efficiency", "dl_throughput_efficiency_rate",
    "drop_rate_percentage", "tsi_value", "go_goroutines", "go_heap_alloc_bytes", "gc_pressure_rate"
]

def main():
    consumer = Consumer({
        'bootstrap.servers': KAFKA_BROKER,
        'group.id': 'pipeline-group',
        'auto.offset.reset': 'latest'
    })
    producer = Producer({'bootstrap.servers': KAFKA_BROKER})

    consumer.subscribe(['upf.metrics.raw'])
    log.info(f"Subscribed to upf.metrics.raw at {KAFKA_BROKER}")

    while True:
        msg = consumer.poll(1.0)
        if msg is None:
            continue
        if msg.error():
            log.error(f"Consumer error: {msg.error()}")
            continue

        try:
            data = json.loads(msg.value().decode('utf-8'))
            # Expecting data to contain the 14 channels
            for i, name in enumerate(channel_names):
                val = data.get(name, 0.0)
                channels_state[i].append(val)
            
            # If window is full, call Ray Serve ML Layer
            if len(channels_state[0]) == window_size:
                channels_list = [list(channels_state[i]) for i in range(14)]
                req_data = {
                    "channels": channels_list,
                    "channel_names": channel_names
                }
                
                resp = requests.post(f"{RAY_SERVE_URL}/detect", json=req_data)
                if resp.status_code == 200:
                    result = resp.json()
                    if result.get("anomaly"):
                        log.info(f"Anomaly detected! Score: {result.get('anomaly_score')}")
                        producer.produce('upf.anomalies.critical', json.dumps(result).encode('utf-8'))
                        producer.flush()
        except Exception as e:
            log.error(f"Processing error: {e}")

if __name__ == '__main__':
    main()
