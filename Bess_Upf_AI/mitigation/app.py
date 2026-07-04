from __future__ import annotations
import json
import logging
import os
import threading
import time

import uvicorn
from confluent_kafka import Consumer, Producer

from mitigation import api as api_module
from mitigation.approval import ApprovalStore, PendingApproval
from mitigation.audit import ActionAuditRecord, AuditPublisher, AUDIT_TOPIC
from mitigation.catalog import execute, rollback
from mitigation.guardrails import BlastRadiusGuard, GuardrailsEngine, RateLimiter
from mitigation.policy import ActionClass, TrustLevel, classify_anomaly

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

KAFKA_BROKER = os.getenv("KAFKA_BROKER", "redpanda:9092")
API_PORT     = int(os.getenv("API_PORT", "8081"))


def _make_record(
    action_id: str,
    event: dict,
    action_class: ActionClass,
    trust_level: TrustLevel,
    dry_run: bool,
    success: bool,
    message: str,
    rollback_token: str,
) -> ActionAuditRecord:
    return ActionAuditRecord(
        action_id=action_id,
        upf_id=event.get("upf_id", "unknown"),
        ts=float(event.get("ts", time.time())),
        action_class=action_class,
        trust_level=trust_level,
        dry_run=dry_run,
        success=success,
        message=message,
        anomaly_score=float(event.get("anomaly_score", 0.0)),
        model_version=str(event.get("model_version", "unknown")),
        rollback_token=rollback_token,
    )


def _handle_event(
    event: dict,
    guardrails: GuardrailsEngine,
    audit: AuditPublisher,
    store: ApprovalStore,
) -> None:
    action_class, trust_level = classify_anomaly(event)
    if action_class == ActionClass.NO_ACTION:
        return

    upf_id = event.get("upf_id", "unknown")

    if trust_level in (TrustLevel.OBSERVE, TrustLevel.RECOMMEND):
        result = execute(action_class, upf_id, dry_run=True)
        audit.publish(_make_record(
            result.action_id, event, action_class, trust_level,
            dry_run=True, success=result.success,
            message=result.message, rollback_token=result.rollback_token,
        ))

    elif trust_level == TrustLevel.APPROVE:
        store.add(PendingApproval(
            action_id=f"{upf_id}-{int(time.time())}",
            action_class=action_class,
            upf_id=upf_id,
            anomaly_score=float(event.get("anomaly_score", 0.0)),
            model_version=str(event.get("model_version", "unknown")),
            rca_summary=f"anomaly_score={event.get('anomaly_score')}",
        ))

    elif trust_level == TrustLevel.AUTO:
        if not guardrails.check(action_class, upf_id):
            log.warning("guardrails blocked: action=%s upf=%s", action_class, upf_id)
            return
        result = execute(action_class, upf_id, dry_run=False)
        audit.publish(_make_record(
            result.action_id, event, action_class, trust_level,
            dry_run=False, success=result.success,
            message=result.message, rollback_token=result.rollback_token,
        ))
        if not result.success:
            rollback(result.rollback_token)


def main() -> None:
    store = ApprovalStore()
    api_module.init(store)

    try:
        producer = Producer({"bootstrap.servers": KAFKA_BROKER})
    except Exception:
        log.warning("Kafka producer unavailable; audit disabled")
        producer = None

    audit = AuditPublisher(producer=producer, topic=AUDIT_TOPIC)
    guardrails = GuardrailsEngine(
        RateLimiter(max_per_window=3, window_seconds=300.0),
        BlastRadiusGuard(max_upfs=5, window_seconds=300.0),
    )

    threading.Thread(
        target=lambda: uvicorn.run(api_module.app, host="0.0.0.0", port=API_PORT, log_level="warning"),
        daemon=True,
    ).start()
    log.info("Approval API running on port %d", API_PORT)

    consumer = Consumer({
        "bootstrap.servers": KAFKA_BROKER,
        "group.id": "mitigation-worker-group",
        "auto.offset.reset": "latest",
    })
    consumer.subscribe(["upf.anomalies.critical"])
    log.info("Mitigation worker subscribed to upf.anomalies.critical")

    while True:
        msg = consumer.poll(1.0)
        if msg is None:
            continue
        if msg.error():
            log.error("Consumer error: %s", msg.error())
            continue
        try:
            _handle_event(json.loads(msg.value().decode("utf-8")), guardrails, audit, store)
        except Exception:
            log.exception("Error processing event")


if __name__ == "__main__":
    main()
