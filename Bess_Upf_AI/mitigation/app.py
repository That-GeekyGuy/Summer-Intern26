import os
import json
import logging
import time
from confluent_kafka import Consumer

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

KAFKA_BROKER = os.getenv("KAFKA_BROKER", "redpanda:9092")

def execute_mitigation(anomaly_event):
    log.info(f"Executing CLOSED-LOOP MITIGATION for anomaly: {anomaly_event.get('anomaly_score')}")
    # Example mitigation actions based on the plan
    top_channels = anomaly_event.get("top_anomalous_channels", [])
    if "port_dropped_N6_rx_rate" in top_channels or "port_dropped_N3_rx_rate" in top_channels:
        log.info("Action: Applying XDP rate limits to aggressive IP blocks (Simulated)")
        time.sleep(1)
        log.info("Action: Scaling up UPF worker instances (Simulated HPA Trigger)")
    elif "pfcp_session_setup_rate" in top_channels:
        log.info("Action: Re-routing new session requests to secondary UPF cluster (Simulated)")
    else:
        log.info("Action: Generic capacity scaling (Simulated)")
        
    log.info("Mitigation successfully executed.")

def main():
    consumer = Consumer({
        'bootstrap.servers': KAFKA_BROKER,
        'group.id': 'mitigation-worker-group',
        'auto.offset.reset': 'latest'
    })

    consumer.subscribe(['upf.anomalies.critical'])
    log.info(f"Mitigation Worker subscribed to upf.anomalies.critical at {KAFKA_BROKER}")

    while True:
        msg = consumer.poll(1.0)
        if msg is None:
            continue
        if msg.error():
            log.error(f"Consumer error: {msg.error()}")
            continue

        try:
            anomaly_event = json.loads(msg.value().decode('utf-8'))
            execute_mitigation(anomaly_event)
        except Exception as e:
            log.error(f"Error processing anomaly for mitigation: {e}")

if __name__ == '__main__':
    main()
