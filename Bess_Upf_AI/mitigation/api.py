from __future__ import annotations
from dataclasses import asdict
from typing import Optional

from fastapi import FastAPI, HTTPException

from mitigation.approval import ApprovalStore

app = FastAPI()
_store: Optional[ApprovalStore] = None


def init(store: ApprovalStore) -> None:
    global _store
    _store = store


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
