import pytest
from mitigation.approval import ApprovalStore, PendingApproval
from mitigation.api import app, init
from mitigation.policy import ActionClass
from fastapi.testclient import TestClient


def _make_approval(**overrides) -> PendingApproval:
    defaults = dict(
        action_id="aid-1",
        action_class=ActionClass.HPA_SCALE_UP,
        upf_id="upf-1",
        anomaly_score=0.9,
        model_version="v1",
        rca_summary="anomaly_score=0.9",
    )
    defaults.update(overrides)
    return PendingApproval(**defaults)


def test_store_add_and_get():
    store = ApprovalStore()
    store.add(_make_approval())
    result = store.get("aid-1")
    assert result is not None
    assert result.status == "pending"


def test_store_list_pending():
    store = ApprovalStore()
    store.add(_make_approval(action_id="a1"))
    store.add(_make_approval(action_id="a2"))
    assert len(store.list_pending()) == 2


def test_store_decide_approve():
    store = ApprovalStore()
    store.add(_make_approval())
    result = store.decide("aid-1", approved=True)
    assert result is not None
    assert result.status == "approved"
    assert result.decided_at is not None


def test_store_decide_deny():
    store = ApprovalStore()
    store.add(_make_approval())
    result = store.decide("aid-1", approved=False)
    assert result.status == "denied"


def test_store_decide_already_decided_returns_none():
    store = ApprovalStore()
    store.add(_make_approval())
    store.decide("aid-1", approved=True)
    assert store.decide("aid-1", approved=False) is None


def test_store_decide_missing_returns_none():
    store = ApprovalStore()
    assert store.decide("nonexistent", approved=True) is None


def test_api_health():
    init(ApprovalStore())
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_api_pending_empty():
    init(ApprovalStore())
    client = TestClient(app)
    resp = client.get("/pending")
    assert resp.status_code == 200
    assert resp.json() == []


def test_api_approve_not_found():
    init(ApprovalStore())
    client = TestClient(app)
    assert client.post("/approve/nonexistent").status_code == 404


def test_api_deny_not_found():
    init(ApprovalStore())
    client = TestClient(app)
    assert client.post("/deny/nonexistent").status_code == 404


def test_api_approve_existing():
    store = ApprovalStore()
    store.add(_make_approval(action_id="aid-x"))
    init(store)
    client = TestClient(app)
    assert client.post("/approve/aid-x").status_code == 200
