# Phase 3: Safe Closed-Loop Mitigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a policy-driven mitigation engine with a trust ladder (OBSERVE→RECOMMEND→APPROVE→AUTO), blast-radius guardrails, human approval gate, and immutable ClickHouse audit — exiting with HPA scale-up running AUTO in sim with full guardrails.

**Architecture:** Kafka consumer loop in `mitigation/app.py` calls `policy.classify_anomaly()` → dispatches to trust-level handler → `catalog.execute()` → `audit.publish()`. FastAPI approval gate runs as daemon thread on port 8081. ClickHouse ingests audit records via Kafka engine → materialized view → MergeTree, same pattern as Phase 2 shadow detections.

**Tech Stack:** Python 3.10, confluent-kafka, FastAPI, uvicorn, ClickHouse Kafka engine, Redpanda (existing), Ray Serve (existing)

---

### Task 1: Policy engine — `mitigation/policy.py`

**Files:**
- Create: `mitigation/policy.py`
- Test: `mitigation/tests/test_policy.py`

- [ ] **Step 1: Write failing tests**

Create `mitigation/tests/__init__.py` (empty) and `mitigation/tests/test_policy.py`:

```python
import pytest
from mitigation.policy import (
    ActionClass, TrustLevel, classify_anomaly, MIN_CONFIDENCE
)

def test_low_score_returns_no_action():
    event = {"anomaly_score": 0.3, "top_anomalous_channels": ["pfcp_sessions_total"]}
    action, trust = classify_anomaly(event)
    assert action == ActionClass.NO_ACTION
    assert trust == TrustLevel.OBSERVE

def test_drop_channels_map_to_xdp():
    event = {"anomaly_score": 0.9, "top_anomalous_channels": ["port_dropped_N6_rx_rate"]}
    action, trust = classify_anomaly(event)
    assert action == ActionClass.XDP_RATE_LIMIT
    assert trust == TrustLevel.OBSERVE

def test_pfcp_channels_map_to_reroute():
    event = {"anomaly_score": 0.8, "top_anomalous_channels": ["pfcp_sessions_total"]}
    action, trust = classify_anomaly(event)
    assert action == ActionClass.PFCP_REROUTE
    assert trust == TrustLevel.OBSERVE

def test_unknown_channels_map_to_hpa():
    event = {"anomaly_score": 0.75, "top_anomalous_channels": ["some_other_channel"]}
    action, trust = classify_anomaly(event)
    assert action == ActionClass.HPA_SCALE_UP
    assert trust == TrustLevel.OBSERVE

def test_missing_channels_defaults_to_hpa():
    event = {"anomaly_score": 0.9}
    action, trust = classify_anomaly(event)
    assert action == ActionClass.HPA_SCALE_UP

def test_boundary_score_exactly_min_confidence():
    event = {"anomaly_score": MIN_CONFIDENCE, "top_anomalous_channels": []}
    action, trust = classify_anomaly(event)
    assert action == ActionClass.HPA_SCALE_UP
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd mitigation && python -m pytest tests/test_policy.py -v
```
Expected: `ModuleNotFoundError: No module named 'mitigation.policy'`

- [ ] **Step 3: Create `mitigation/policy.py`**

```python
from __future__ import annotations
from enum import Enum


class ActionClass(str, Enum):
    HPA_SCALE_UP   = "hpa_scale_up"
    XDP_RATE_LIMIT = "xdp_rate_limit"
    PFCP_REROUTE   = "pfcp_reroute"
    NO_ACTION      = "no_action"


class TrustLevel(str, Enum):
    OBSERVE   = "observe"
    RECOMMEND = "recommend"
    APPROVE   = "approve"
    AUTO      = "auto"


TRUST_LEVELS: dict[ActionClass, TrustLevel] = {
    ActionClass.HPA_SCALE_UP:   TrustLevel.OBSERVE,
    ActionClass.XDP_RATE_LIMIT: TrustLevel.OBSERVE,
    ActionClass.PFCP_REROUTE:   TrustLevel.OBSERVE,
}

MIN_CONFIDENCE = 0.6

_DROP_CHANNELS  = {"port_dropped_N6_rx_rate", "port_dropped_N3_rx_rate"}
_PFCP_CHANNELS  = {"pfcp_sessions_total", "pfcp_session_setup_rate"}


def classify_anomaly(event: dict) -> tuple[ActionClass, TrustLevel]:
    score = float(event.get("anomaly_score", 0.0))
    if score < MIN_CONFIDENCE:
        return ActionClass.NO_ACTION, TrustLevel.OBSERVE
    top_ch: list[str] = event.get("top_anomalous_channels") or []
    if any(ch in _DROP_CHANNELS for ch in top_ch):
        action = ActionClass.XDP_RATE_LIMIT
    elif any(ch in _PFCP_CHANNELS for ch in top_ch):
        action = ActionClass.PFCP_REROUTE
    else:
        action = ActionClass.HPA_SCALE_UP
    return action, TRUST_LEVELS.get(action, TrustLevel.OBSERVE)
```

Also create `mitigation/__init__.py` (empty) so imports work.

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd mitigation && python -m pytest tests/test_policy.py -v
```
Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add mitigation/__init__.py mitigation/policy.py mitigation/tests/__init__.py mitigation/tests/test_policy.py
git commit -m "feat(mitigation): policy engine with trust ladder and action classification"
```

---

### Task 2: Action catalog — `mitigation/catalog.py`

**Files:**
- Create: `mitigation/catalog.py`
- Test: `mitigation/tests/test_catalog.py`

- [ ] **Step 1: Write failing tests**

```python
import pytest
from mitigation.catalog import execute, rollback, ActuatorResult
from mitigation.policy import ActionClass


def test_hpa_dry_run_returns_result():
    result = execute(ActionClass.HPA_SCALE_UP, "upf-1", dry_run=True)
    assert isinstance(result, ActuatorResult)
    assert result.success
    assert result.dry_run
    assert result.action_class == ActionClass.HPA_SCALE_UP
    assert result.upf_id == "upf-1"
    assert result.rollback_token.startswith("hpa:upf-1:")


def test_xdp_dry_run():
    result = execute(ActionClass.XDP_RATE_LIMIT, "upf-2", dry_run=True)
    assert result.success
    assert result.action_class == ActionClass.XDP_RATE_LIMIT


def test_pfcp_dry_run():
    result = execute(ActionClass.PFCP_REROUTE, "upf-3", dry_run=True)
    assert result.success
    assert result.action_class == ActionClass.PFCP_REROUTE


def test_no_action_returns_no_op():
    result = execute(ActionClass.NO_ACTION, "upf-4", dry_run=True)
    assert result.action_class == ActionClass.NO_ACTION
    assert result.message == "no_action"


def test_rollback_does_not_raise():
    rollback("hpa:upf-1:some-id")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd mitigation && python -m pytest tests/test_catalog.py -v
```
Expected: `ModuleNotFoundError: No module named 'mitigation.catalog'`

- [ ] **Step 3: Create `mitigation/catalog.py`**

```python
from __future__ import annotations
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from mitigation.policy import ActionClass

log = logging.getLogger(__name__)


@dataclass
class ActuatorResult:
    action_id:      str
    action_class:   ActionClass
    upf_id:         str
    dry_run:        bool
    success:        bool
    message:        str
    rollback_token: str


def _sim_hpa(upf_id: str, dry_run: bool) -> ActuatorResult:
    action_id = str(uuid.uuid4())
    if not dry_run:
        log.info("HPA scale-up: upf=%s", upf_id)
        time.sleep(0.05)
    return ActuatorResult(
        action_id=action_id,
        action_class=ActionClass.HPA_SCALE_UP,
        upf_id=upf_id,
        dry_run=dry_run,
        success=True,
        message="hpa_scale_up_simulated",
        rollback_token=f"hpa:{upf_id}:{action_id}",
    )


def _sim_xdp(upf_id: str, dry_run: bool) -> ActuatorResult:
    action_id = str(uuid.uuid4())
    if not dry_run:
        log.info("XDP rate-limit: upf=%s", upf_id)
        time.sleep(0.05)
    return ActuatorResult(
        action_id=action_id,
        action_class=ActionClass.XDP_RATE_LIMIT,
        upf_id=upf_id,
        dry_run=dry_run,
        success=True,
        message="xdp_rate_limit_simulated",
        rollback_token=f"xdp:{upf_id}:{action_id}",
    )


def _sim_pfcp(upf_id: str, dry_run: bool) -> ActuatorResult:
    action_id = str(uuid.uuid4())
    if not dry_run:
        log.info("PFCP reroute: upf=%s", upf_id)
        time.sleep(0.05)
    return ActuatorResult(
        action_id=action_id,
        action_class=ActionClass.PFCP_REROUTE,
        upf_id=upf_id,
        dry_run=dry_run,
        success=True,
        message="pfcp_reroute_simulated",
        rollback_token=f"pfcp:{upf_id}:{action_id}",
    )


def _no_op(upf_id: str, dry_run: bool) -> ActuatorResult:
    return ActuatorResult(
        action_id=str(uuid.uuid4()),
        action_class=ActionClass.NO_ACTION,
        upf_id=upf_id,
        dry_run=dry_run,
        success=True,
        message="no_action",
        rollback_token="",
    )


_CATALOG: dict[ActionClass, Callable[[str, bool], ActuatorResult]] = {
    ActionClass.HPA_SCALE_UP:   _sim_hpa,
    ActionClass.XDP_RATE_LIMIT: _sim_xdp,
    ActionClass.PFCP_REROUTE:   _sim_pfcp,
    ActionClass.NO_ACTION:      _no_op,
}


def execute(action_class: ActionClass, upf_id: str, dry_run: bool = True) -> ActuatorResult:
    fn = _CATALOG.get(action_class, _no_op)
    return fn(upf_id, dry_run)


def rollback(rollback_token: str) -> None:
    log.info("Rollback requested: token=%s", rollback_token)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd mitigation && python -m pytest tests/test_catalog.py -v
```
Expected: `5 passed`

- [ ] **Step 5: Commit**

```bash
git add mitigation/catalog.py mitigation/tests/test_catalog.py
git commit -m "feat(mitigation): action catalog with HPA/XDP/PFCP sim actuators"
```

---

### Task 3: Guardrails engine — `mitigation/guardrails.py`

**Files:**
- Create: `mitigation/guardrails.py`
- Test: `mitigation/tests/test_guardrails.py`

- [ ] **Step 1: Write failing tests**

```python
import pytest
from mitigation.guardrails import RateLimiter, BlastRadiusGuard, GuardrailsEngine
from mitigation.policy import ActionClass


def test_rate_limiter_allows_within_window():
    rl = RateLimiter(max_per_window=3, window_seconds=60.0)
    assert rl.check(ActionClass.HPA_SCALE_UP, "upf-1")
    assert rl.check(ActionClass.HPA_SCALE_UP, "upf-1")
    assert rl.check(ActionClass.HPA_SCALE_UP, "upf-1")


def test_rate_limiter_blocks_over_limit():
    rl = RateLimiter(max_per_window=2, window_seconds=60.0)
    rl.check(ActionClass.HPA_SCALE_UP, "upf-1")
    rl.check(ActionClass.HPA_SCALE_UP, "upf-1")
    assert not rl.check(ActionClass.HPA_SCALE_UP, "upf-1")


def test_blast_radius_allows_within_limit():
    bg = BlastRadiusGuard(max_upfs=3, window_seconds=60.0)
    assert bg.check(ActionClass.HPA_SCALE_UP, "upf-1")
    assert bg.check(ActionClass.HPA_SCALE_UP, "upf-2")
    assert bg.check(ActionClass.HPA_SCALE_UP, "upf-3")


def test_blast_radius_blocks_over_limit():
    bg = BlastRadiusGuard(max_upfs=2, window_seconds=60.0)
    bg.check(ActionClass.HPA_SCALE_UP, "upf-1")
    bg.check(ActionClass.HPA_SCALE_UP, "upf-2")
    assert not bg.check(ActionClass.HPA_SCALE_UP, "upf-3")


def test_blast_radius_same_upf_does_not_count_twice():
    bg = BlastRadiusGuard(max_upfs=1, window_seconds=60.0)
    assert bg.check(ActionClass.HPA_SCALE_UP, "upf-1")
    assert bg.check(ActionClass.HPA_SCALE_UP, "upf-1")


def test_guardrails_engine_passes_clean():
    engine = GuardrailsEngine(
        RateLimiter(max_per_window=3, window_seconds=60.0),
        BlastRadiusGuard(max_upfs=5, window_seconds=60.0),
    )
    assert engine.check(ActionClass.HPA_SCALE_UP, "upf-1")


def test_guardrails_engine_blocks_on_rate():
    engine = GuardrailsEngine(
        RateLimiter(max_per_window=1, window_seconds=60.0),
        BlastRadiusGuard(max_upfs=5, window_seconds=60.0),
    )
    engine.check(ActionClass.HPA_SCALE_UP, "upf-1")
    assert not engine.check(ActionClass.HPA_SCALE_UP, "upf-1")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd mitigation && python -m pytest tests/test_guardrails.py -v
```
Expected: `ModuleNotFoundError: No module named 'mitigation.guardrails'`

- [ ] **Step 3: Create `mitigation/guardrails.py`**

```python
from __future__ import annotations
import time
from collections import defaultdict

from mitigation.policy import ActionClass


class RateLimiter:
    def __init__(self, max_per_window: int = 3, window_seconds: float = 300.0) -> None:
        self._max = max_per_window
        self._window = window_seconds
        self._hits: dict[tuple, list[float]] = defaultdict(list)

    def check(self, action_class: ActionClass, upf_id: str) -> bool:
        key = (action_class, upf_id)
        now = time.monotonic()
        self._hits[key] = [t for t in self._hits[key] if now - t < self._window]
        if len(self._hits[key]) >= self._max:
            return False
        self._hits[key].append(now)
        return True


class BlastRadiusGuard:
    def __init__(self, max_upfs: int = 5, window_seconds: float = 300.0) -> None:
        self._max = max_upfs
        self._window = window_seconds
        self._events: dict[ActionClass, list[tuple[float, str]]] = defaultdict(list)

    def check(self, action_class: ActionClass, upf_id: str) -> bool:
        now = time.monotonic()
        self._events[action_class] = [
            (t, u) for t, u in self._events[action_class] if now - t < self._window
        ]
        seen = {u for _, u in self._events[action_class]}
        if upf_id not in seen and len(seen) >= self._max:
            return False
        self._events[action_class].append((now, upf_id))
        return True


class GuardrailsEngine:
    def __init__(self, rate_limiter: RateLimiter, blast_guard: BlastRadiusGuard) -> None:
        self._rl = rate_limiter
        self._bg = blast_guard

    def check(self, action_class: ActionClass, upf_id: str) -> bool:
        return self._rl.check(action_class, upf_id) and self._bg.check(action_class, upf_id)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd mitigation && python -m pytest tests/test_guardrails.py -v
```
Expected: `8 passed`

- [ ] **Step 5: Commit**

```bash
git add mitigation/guardrails.py mitigation/tests/test_guardrails.py
git commit -m "feat(mitigation): rate limiter + blast-radius guard with sliding window"
```

---

### Task 4: Audit publisher + ClickHouse DDL — `mitigation/audit.py` + `config/clickhouse/init.sql`

**Files:**
- Create: `mitigation/audit.py`
- Modify: `config/clickhouse/init.sql` (append DDL)
- Test: `mitigation/tests/test_audit.py`

- [ ] **Step 1: Write failing tests**

```python
import json
import pytest
from unittest.mock import MagicMock
from mitigation.audit import ActionAuditRecord, AuditPublisher, AUDIT_TOPIC
from mitigation.policy import ActionClass, TrustLevel


def _make_record(**overrides) -> ActionAuditRecord:
    defaults = dict(
        action_id="aid-1",
        upf_id="upf-1",
        ts=1.0,
        action_class=ActionClass.HPA_SCALE_UP,
        trust_level=TrustLevel.OBSERVE,
        dry_run=True,
        success=True,
        message="ok",
        anomaly_score=0.9,
        model_version="v1",
        rollback_token="tok",
    )
    defaults.update(overrides)
    return ActionAuditRecord(**defaults)


def test_publish_sends_to_correct_topic():
    producer = MagicMock()
    pub = AuditPublisher(producer=producer, topic=AUDIT_TOPIC)
    pub.publish(_make_record())
    producer.produce.assert_called_once()
    call_kwargs = producer.produce.call_args
    assert call_kwargs[1].get("topic") == AUDIT_TOPIC or call_kwargs[0][0] == AUDIT_TOPIC


def test_publish_serializes_booleans_as_int():
    producer = MagicMock()
    pub = AuditPublisher(producer=producer)
    pub.publish(_make_record(dry_run=True, success=False))
    raw = producer.produce.call_args[1].get("value") or producer.produce.call_args[0][1]
    payload = json.loads(raw)
    assert payload["dry_run"] == 1
    assert payload["success"] == 0


def test_publish_no_nan_in_payload():
    producer = MagicMock()
    pub = AuditPublisher(producer=producer)
    pub.publish(_make_record(anomaly_score=float("nan")))
    raw = producer.produce.call_args[1].get("value") or producer.produce.call_args[0][1]
    assert b"NaN" not in raw


def test_publish_swallows_producer_exception():
    producer = MagicMock()
    producer.produce.side_effect = Exception("kafka down")
    pub = AuditPublisher(producer=producer)
    pub.publish(_make_record())  # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd mitigation && python -m pytest tests/test_audit.py -v
```
Expected: `ModuleNotFoundError: No module named 'mitigation.audit'`

- [ ] **Step 3: Create `mitigation/audit.py`**

```python
from __future__ import annotations
import json
import logging
import math
import time
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
```

- [ ] **Step 4: Append action_audit DDL to `config/clickhouse/init.sql`**

Read the current file, then append at the end:

```sql

-- Phase 3: action audit
CREATE TABLE IF NOT EXISTS action_audit_kafka (
    action_id      String,
    upf_id         String,
    ts             Float64,
    action_class   String,
    trust_level    String,
    dry_run        UInt8,
    success        UInt8,
    message        String,
    anomaly_score  Float64,
    model_version  String,
    rollback_token String
) ENGINE = Kafka SETTINGS
    kafka_broker_list = 'redpanda:9092',
    kafka_topic_list  = 'upf.action_audit',
    kafka_group_name  = 'clickhouse-audit',
    kafka_format      = 'JSONEachRow',
    kafka_skip_broken_messages = 1;

CREATE TABLE IF NOT EXISTS action_audit (
    action_id      String,
    upf_id         LowCardinality(String),
    ts             DateTime64(3),
    action_class   LowCardinality(String),
    trust_level    LowCardinality(String),
    dry_run        UInt8,
    success        UInt8,
    message        String,
    anomaly_score  Float64,
    model_version  LowCardinality(String),
    rollback_token String
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(ts)
ORDER BY (upf_id, ts)
TTL toDateTime(ts) + INTERVAL 365 DAY;

CREATE MATERIALIZED VIEW IF NOT EXISTS action_audit_mv TO action_audit AS
SELECT
    action_id,
    upf_id,
    toDateTime64(ts, 3) AS ts,
    action_class,
    trust_level,
    dry_run,
    success,
    message,
    anomaly_score,
    model_version,
    rollback_token
FROM action_audit_kafka;
```

- [ ] **Step 5: Run audit tests**

```bash
cd mitigation && python -m pytest tests/test_audit.py -v
```
Expected: `4 passed`

- [ ] **Step 6: Commit**

```bash
git add mitigation/audit.py mitigation/tests/test_audit.py config/clickhouse/init.sql
git commit -m "feat(mitigation+ch): audit publisher; action_audit Kafka→MergeTree DDL"
```

---

### Task 5: Approval store + FastAPI gate — `mitigation/approval.py` + `mitigation/api.py`

**Files:**
- Create: `mitigation/approval.py`
- Create: `mitigation/api.py`
- Modify: `mitigation/requirements.txt` (add fastapi, uvicorn, httpx)
- Test: `mitigation/tests/test_approval.py`

- [ ] **Step 1: Write failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd mitigation && python -m pytest tests/test_approval.py -v
```
Expected: `ModuleNotFoundError: No module named 'mitigation.approval'`

- [ ] **Step 3: Create `mitigation/approval.py`**

```python
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
```

- [ ] **Step 4: Create `mitigation/api.py`**

```python
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
```

- [ ] **Step 5: Update `mitigation/requirements.txt`**

```
confluent-kafka
fastapi
uvicorn
httpx
```

- [ ] **Step 6: Run tests**

```bash
pip install fastapi uvicorn httpx && cd mitigation && python -m pytest tests/test_approval.py -v
```
Expected: `11 passed`

- [ ] **Step 7: Commit**

```bash
git add mitigation/approval.py mitigation/api.py mitigation/requirements.txt mitigation/tests/test_approval.py
git commit -m "feat(mitigation): approval store + FastAPI gate on /health /pending /approve /deny"
```

---

### Task 6: Wire everything — rewrite `mitigation/app.py` + update Dockerfile + compose

**Files:**
- Rewrite: `mitigation/app.py`
- Modify: `mitigation/Dockerfile`
- Modify: `docker-compose.v2.yml`
- Test: `mitigation/tests/test_app.py`

- [ ] **Step 1: Write failing tests for `_handle_event`**

```python
import json
import pytest
from unittest.mock import MagicMock, patch
from mitigation.policy import ActionClass, TrustLevel, TRUST_LEVELS
from mitigation.guardrails import GuardrailsEngine, RateLimiter, BlastRadiusGuard
from mitigation.audit import AuditPublisher
from mitigation.approval import ApprovalStore
from mitigation.app import _handle_event


def _make_event(**overrides):
    base = {
        "upf_id": "upf-1",
        "ts": 1.0,
        "anomaly_score": 0.9,
        "top_anomalous_channels": ["some_channel"],
        "model_version": "v1",
    }
    base.update(overrides)
    return base


def _make_engine():
    return GuardrailsEngine(
        RateLimiter(max_per_window=10, window_seconds=300.0),
        BlastRadiusGuard(max_upfs=10, window_seconds=300.0),
    )


def test_observe_runs_dry_run():
    producer = MagicMock()
    audit = AuditPublisher(producer=producer)
    store = ApprovalStore()
    engine = _make_engine()

    with patch.dict(TRUST_LEVELS, {ActionClass.HPA_SCALE_UP: TrustLevel.OBSERVE}):
        _handle_event(_make_event(), engine, audit, store)

    producer.produce.assert_called_once()
    d = json.loads(producer.produce.call_args[1].get("value") or producer.produce.call_args[0][1])
    assert d["dry_run"] == 1


def test_approve_queues_in_store():
    audit = AuditPublisher(producer=None)
    store = ApprovalStore()
    engine = _make_engine()

    with patch.dict(TRUST_LEVELS, {ActionClass.HPA_SCALE_UP: TrustLevel.APPROVE}):
        _handle_event(_make_event(), engine, audit, store)

    assert len(store.list_pending()) == 1


def test_auto_blocked_by_guardrails():
    producer = MagicMock()
    audit = AuditPublisher(producer=producer)
    store = ApprovalStore()
    engine = GuardrailsEngine(
        RateLimiter(max_per_window=0, window_seconds=300.0),
        BlastRadiusGuard(max_upfs=10, window_seconds=300.0),
    )

    with patch.dict(TRUST_LEVELS, {ActionClass.HPA_SCALE_UP: TrustLevel.AUTO}):
        _handle_event(_make_event(), engine, audit, store)

    producer.produce.assert_not_called()


def test_auto_executes_and_audits():
    producer = MagicMock()
    audit = AuditPublisher(producer=producer)
    store = ApprovalStore()
    engine = _make_engine()

    with patch.dict(TRUST_LEVELS, {ActionClass.HPA_SCALE_UP: TrustLevel.AUTO}):
        _handle_event(_make_event(), engine, audit, store)

    producer.produce.assert_called_once()
    d = json.loads(producer.produce.call_args[1].get("value") or producer.produce.call_args[0][1])
    assert d["dry_run"] == 0
    assert d["trust_level"] == "auto"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd mitigation && python -m pytest tests/test_app.py -v
```
Expected: `ImportError: cannot import name '_handle_event' from 'mitigation.app'`

- [ ] **Step 3: Rewrite `mitigation/app.py`**

```python
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
```

- [ ] **Step 4: Update `mitigation/Dockerfile`**

Change `COPY app.py .` → `COPY *.py ./`:

```dockerfile
FROM python:3.10-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

CMD ["python", "app.py"]
```

- [ ] **Step 5: Update `docker-compose.v2.yml` mitigation service**

Find the mitigation service block. Add `API_PORT` and expose port 8081:

```yaml
    environment:
      - KAFKA_BROKER=redpanda:9092
      - API_PORT=8081
    ports:
      - "8081:8081"
```

- [ ] **Step 6: Run tests**

```bash
cd mitigation && python -m pytest tests/test_app.py -v
```
Expected: `4 passed`

- [ ] **Step 7: Run all mitigation tests**

```bash
cd mitigation && python -m pytest tests/ -v
```
Expected: all 28+ tests green

- [ ] **Step 8: Commit**

```bash
git add mitigation/app.py mitigation/Dockerfile docker-compose.v2.yml mitigation/tests/test_app.py
git commit -m "feat(mitigation): wire policy→guardrails→catalog→audit→approval; FastAPI daemon on 8081"
```

---

### Task 7: Promote HPA to AUTO + integration smoke

**Files:**
- Modify: `mitigation/policy.py` (one line: OBSERVE → AUTO for HPA_SCALE_UP)
- Create: `mitigation/tests/test_integration_smoke.py`

- [ ] **Step 1: Write failing integration smoke**

```python
import json
import pytest
from unittest.mock import MagicMock
from mitigation.policy import ActionClass, TrustLevel, TRUST_LEVELS
from mitigation.guardrails import GuardrailsEngine, RateLimiter, BlastRadiusGuard
from mitigation.audit import AuditPublisher
from mitigation.approval import ApprovalStore
from mitigation.app import _handle_event


def _engine(max_rate=3, max_upfs=5):
    return GuardrailsEngine(
        RateLimiter(max_per_window=max_rate, window_seconds=300.0),
        BlastRadiusGuard(max_upfs=max_upfs, window_seconds=300.0),
    )


def test_hpa_is_auto():
    assert TRUST_LEVELS[ActionClass.HPA_SCALE_UP] == TrustLevel.AUTO


def test_hpa_auto_full_path():
    producer = MagicMock()
    audit = AuditPublisher(producer=producer)
    event = {"upf_id": "upf-sim-1", "ts": 1.0, "anomaly_score": 0.85,
             "top_anomalous_channels": ["cpu_usage"], "model_version": "v1"}
    _handle_event(event, _engine(), audit, ApprovalStore())

    producer.produce.assert_called_once()
    d = json.loads(producer.produce.call_args[1].get("value") or producer.produce.call_args[0][1])
    assert d["dry_run"] == 0
    assert d["trust_level"] == "auto"
    assert d["action_class"] == "hpa_scale_up"
    assert d["success"] == 1


def test_xdp_still_observe():
    assert TRUST_LEVELS[ActionClass.XDP_RATE_LIMIT] == TrustLevel.OBSERVE


def test_blast_radius_caps_at_5():
    producer = MagicMock()
    audit = AuditPublisher(producer=producer)
    template = {"ts": 1.0, "anomaly_score": 0.9,
                "top_anomalous_channels": ["cpu_usage"], "model_version": "v1"}
    for i in range(6):
        _handle_event({**template, "upf_id": f"upf-{i}"}, _engine(), audit, ApprovalStore())

    assert producer.produce.call_count == 5
```

- [ ] **Step 2: Run `test_hpa_is_auto` to verify it fails**

```bash
cd mitigation && python -m pytest tests/test_integration_smoke.py::test_hpa_is_auto -v
```
Expected: `AssertionError: assert TrustLevel.OBSERVE == TrustLevel.AUTO`

- [ ] **Step 3: Edit `mitigation/policy.py` — one line change**

Change:
```python
    ActionClass.HPA_SCALE_UP:   TrustLevel.OBSERVE,
```
To:
```python
    ActionClass.HPA_SCALE_UP:   TrustLevel.AUTO,
```

- [ ] **Step 4: Run integration smoke**

```bash
cd mitigation && python -m pytest tests/test_integration_smoke.py -v
```
Expected: `4 passed`

- [ ] **Step 5: Run full suite**

```bash
cd mitigation && python -m pytest tests/ -v
```
Expected: all tests pass. (`test_observe_runs_dry_run` in `test_app.py` uses `patch.dict` so still passes.)

- [ ] **Step 6: Commit**

```bash
git add mitigation/policy.py mitigation/tests/test_integration_smoke.py
git commit -m "feat(mitigation): promote HPA_SCALE_UP to AUTO; integration smoke confirms guardrails+blast-radius"
```

---

## Exit Criteria

- [ ] All 7 tasks committed on `phase2-model-serving` branch
- [ ] `python -m pytest mitigation/tests/ -v` → all green
- [ ] `mitigation/policy.py`: `HPA_SCALE_UP = AUTO`, XDP/PFCP = OBSERVE
- [ ] `config/clickhouse/init.sql`: `action_audit` table + Kafka engine + materialized view present
- [ ] `mitigation/Dockerfile`: `COPY *.py ./`
- [ ] `docker-compose.v2.yml`: mitigation has `API_PORT=8081` + `ports: ["8081:8081"]`
- [ ] FastAPI endpoints: `/health`, `/pending`, `/approve/{id}`, `/deny/{id}`
- [ ] Blast radius capped at 5 UPFs / 300s; rate capped at 3 actions / 300s per (action, upf)
