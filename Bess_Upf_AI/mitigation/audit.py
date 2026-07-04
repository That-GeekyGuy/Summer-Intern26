from __future__ import annotations
import json
import logging
import math
from dataclasses import asdict, dataclass

from mitigation.policy import ActionClass, TrustLevel

log = logging.getLogger(__name__)

AUDIT_TOPIC = "upf.action_audit"


@dataclass
class ActionAuditRecord:
    action_id:      str
    upf_id:         str
    ts:             float
    action_class:   ActionClass
    trust_level:    TrustLevel
    dry_run:        bool
    success:        bool
    message:        str
    anomaly_score:  float
    model_version:  str
    rollback_token: str


class AuditPublisher:
    def __init__(self, producer=None, topic: str = AUDIT_TOPIC) -> None:
        self._producer = producer
        self._topic = topic

    def publish(self, record: ActionAuditRecord) -> None:
        if self._producer is None:
            return
        try:
            d = asdict(record)
            d["action_class"] = record.action_class.value
            d["trust_level"]  = record.trust_level.value
            d["dry_run"]      = int(record.dry_run)
            d["success"]      = int(record.success)
            if math.isnan(d.get("anomaly_score", 0.0)):
                d["anomaly_score"] = 0.0
            payload = json.dumps(d, allow_nan=False).encode()
            self._producer.produce(topic=self._topic, value=payload)
            self._producer.poll(0)
        except Exception:
            log.exception("audit publish failed")
