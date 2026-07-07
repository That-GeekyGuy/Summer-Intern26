from __future__ import annotations
from dataclasses import asdict
from typing import Optional

from fastapi import FastAPI, HTTPException

from mitigation.approval import ApprovalStore
from mitigation.audit import AuditPublisher

app = FastAPI()
_store: Optional[ApprovalStore] = None
_audit: Optional[AuditPublisher] = None


def init(store: ApprovalStore, audit: AuditPublisher = None) -> None:
    global _store, _audit
    _store = store
    _audit = audit


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/pending")
def list_pending():
    if _store is None:
        return []
    return [asdict(a) for a in _store.list_pending()]


@app.post("/approve/{action_id}")
def approve(action_id: str):
    if _store is None:
        raise HTTPException(status_code=503, detail="store not initialized")
    result = _store.decide(action_id, approved=True)
    if result is None:
        raise HTTPException(status_code=404, detail="not found or already decided")
    return {"status": result.status}


@app.post("/deny/{action_id}")
def deny(action_id: str):
    if _store is None:
        raise HTTPException(status_code=503, detail="store not initialized")
    result = _store.decide(action_id, approved=False)
    if result is None:
        raise HTTPException(status_code=404, detail="not found or already decided")
    return {"status": result.status}


from pydantic import BaseModel
import time

class FeedbackRequest(BaseModel):
    upf_id: str
    label: str  # e.g., "false_positive", "true_positive"

@app.post("/feedback/{action_id}")
def feedback(action_id: str, req: FeedbackRequest):
    if _audit is None:
        raise HTTPException(status_code=503, detail="audit publisher not initialized")
    
    # We publish this directly to the upf.feedback topic
    msg = {
        "action_id": action_id,
        "upf_id": req.upf_id,
        "label": req.label,
        "ts": time.time()
    }
    _audit.publish_raw("upf.feedback", msg)
    return {"status": "feedback_received"}
