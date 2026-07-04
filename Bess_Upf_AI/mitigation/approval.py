from __future__ import annotations
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from mitigation.policy import ActionClass


@dataclass
class PendingApproval:
    action_id:     str
    action_class:  ActionClass
    upf_id:        str
    anomaly_score: float
    model_version: str
    rca_summary:   str
    created_at:    float = field(default_factory=time.time)
    status:        str   = "pending"
    decided_at:    Optional[float] = None


class ApprovalStore:
    def __init__(self) -> None:
        self._lock:  threading.Lock = threading.Lock()
        self._store: dict[str, PendingApproval] = {}

    def add(self, approval: PendingApproval) -> None:
        with self._lock:
            self._store[approval.action_id] = approval

    def get(self, action_id: str) -> Optional[PendingApproval]:
        with self._lock:
            return self._store.get(action_id)

    def list_pending(self) -> list[PendingApproval]:
        with self._lock:
            return [a for a in self._store.values() if a.status == "pending"]

    def decide(self, action_id: str, approved: bool) -> Optional[PendingApproval]:
        with self._lock:
            pa = self._store.get(action_id)
            if pa is None or pa.status != "pending":
                return None
            pa.status = "approved" if approved else "denied"
            pa.decided_at = time.time()
            return pa
