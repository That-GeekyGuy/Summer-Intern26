import os
import time
import json
import logging
import requests
from confluent_kafka import Producer

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

KAFKA_BROKER = os.getenv("KAFKA_BROKER", "redpanda:9092")
UPF_SIM_METRICS = os.getenv("UPF_SIM_METRICS", "http://upf-sim:8090/metrics")

channel_names = [
    "port_bytes_N3_rx_rate", "port_bytes_N6_tx_rate", "port_pkts_N3_rx_rate",
    "port_dropped_N3_rx_rate", "port_dropped_N6_rx_rate", "pfcp_sessions_total",
    "pfcp_session_setup_rate", "dl_throughput_efficiency", "dl_throughput_efficiency_rate",
    "drop_rate_percentage", "tsi_value", "go_goroutines", "go_heap_alloc_bytes", "gc_pressure_rate"
]

def parse_prometheus_metrics(text):
    data = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        try:
            parts = line.split()
            if len(parts) >= 2:
                metric_name = parts[0].split("{")[0]
                value = float(parts[-1])
                # We need to map standard prom metric names to our channel_names.
                # In a real scenario, we would parse PromQL, but for V2 simulation
                # let's map loosely or just extract numbers.
                data[metric_name] = value
        except Exception:
            pass
            
    # Normalize to our channels (mock mapping if exact metrics aren't exposed)
    channels_data = {}
    for name in channel_names:
        channels_data[name] = data.get(name, 0.0) # Fallback 0.0 if not perfectly matched
    return channels_data

def main():
    producer = Producer({'bootstrap.servers': KAFKA_BROKER})
    log.info(f"Starting scraper for {UPF_SIM_METRICS} -> upf.metrics.raw")
    
    while True:
        try:
            resp = requests.get(UPF_SIM_METRICS, timeout=5)
            if resp.status_code == 200:
                metrics_data = parse_prometheus_metrics(resp.text)
                producer.produce('upf.metrics.raw', json.dumps(metrics_data).encode('utf-8'))
                producer.flush()
        except Exception as e:
            log.warning(f"Failed to scrape metrics: {e}")
        time.sleep(1.0) # sub-second/1s streaming

if __name__ == '__main__':
    main()
