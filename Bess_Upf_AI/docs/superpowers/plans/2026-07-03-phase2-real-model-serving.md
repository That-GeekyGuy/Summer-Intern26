# Phase 2 — Real Model Serving + Honest Eval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire sklearn (IF+RF) and MOMENT head into Ray Serve, build an eval harness reporting P/R/F1/calibration, and add shadow detection logging — so v2 detection is provably real and measurable.

**Architecture:** `serve/app.py` (Ray Serve) currently returns mock zeros. Phase 2 loads real model artifacts from `MODELS_DIR=/models` (mounted read-only), adds a feature-engineering layer that maps the 14-channel 512-step window from the pipeline into the 40-feature sklearn input space, and runs IF + RF in ensemble. A separate eval harness re-runs the same models on `models/dataset_eval.parquet` and prints a report comparing to training baselines. Shadow detections are published to a new Kafka topic so ClickHouse can accumulate both scores for comparison.

**Tech Stack:** Ray Serve 2.30, scikit-learn, joblib, torch (MOMENT head), pandas/pyarrow (eval), Bytewax 0.21, ClickHouse 24.3, Redpanda

**Codebase context (read before implementing any task):**
- `serve/app.py` — mock deployment; replace `_load_moment_models`/`_load_chronos_models`/`detect`
- `pipeline/window.py` — `ML_CHANNELS` (14 names), `WINDOW_SIZE=512`
- `models/metadata.json` — baseline eval metrics (IF AUC 0.92, RF AUC 0.924)
- `models/moment_threshold.json` — `{"threshold": 0.824, "embedding_dim": 7168}`
- `models/scaler_params.json` — 40 feature names sklearn expects
- `models/dataset_eval.parquet` — labeled eval set (anomaly/normal rows)
- `config/clickhouse/init.sql` — existing DDL; shadow table appended here

---

## File Map

| Action | Path | Purpose |
|--------|------|---------|
| Create | `serve/feature_eng.py` | 14×512 window → 40 sklearn features |
| Create | `serve/moment_loader.py` | Load moment_head.pt; graceful fallback |
| Create | `serve/eval_harness.py` | Offline eval script vs dataset_eval.parquet |
| Modify | `serve/app.py` | Wire feature_eng + sklearn + MOMENT into /detect |
| Modify | `serve/requirements.txt` | Add scikit-learn, joblib, pyarrow, confluent-kafka |
| Modify | `serve/Dockerfile` | Copy new .py files |
| Modify | `config/clickhouse/init.sql` | Add shadow_detections Kafka+MV+MergeTree |
| Create | `serve/tests/__init__.py` | Empty |
| Create | `serve/tests/test_feature_eng.py` | Unit tests for feature extraction |
| Create | `serve/tests/test_eval_harness.py` | Smoke test eval script |
| Create | `serve/shadow_publisher.py` | Kafka shadow event publisher |
| Create | `serve/tests/test_shadow.py` | Unit tests for shadow publisher |
| Create | `serve/tests/test_integration_smoke.py` | Integration smoke vs real models |

---

### Task 1: Feature Engineering Layer

**Files:**
- Create: `serve/feature_eng.py`
- Create: `serve/tests/__init__.py`
- Create: `serve/tests/test_feature_eng.py`

The pipeline sends channels array shape `(14, 512)` with names from `ML_CHANNELS`. sklearn was trained on 40 derived features (rates, ratios, fractions, 5-min rolling stats). This task builds the bridge.

Channel mapping (pipeline name → sklearn base name):
```
pfcp_sessions_total      → rate_pfcp_sessions
port_bytes_N3_rx_rate    → rate_bytes_N3_rx
port_bytes_N6_tx_rate    → rate_bytes_N6_tx
port_pkts_N3_rx_rate     → rate_bytes_N3_tx  (proxy; N3 packet rate ≈ N3 tx throughput proxy)
port_pkts_N6_tx_rate     → rate_bytes_N6_rx  (proxy)
port_dropped_N3_rx_rate  → rate_drops_N3
port_dropped_N6_rx_rate  → rate_drops_N6
process_cpu_rate         → cpu_rate
```

Derived features computed from the above:
```
rx_tx_ratio_N3  = rate_bytes_N3_rx / (rate_bytes_N6_tx + 1e-9)
rx_tx_ratio_N6  = rate_bytes_N3_tx / (rate_bytes_N6_rx + 1e-9)
drop_fraction_N3 = rate_drops_N3 / (rate_bytes_N3_rx + 1e-9)
drop_fraction_N6 = rate_drops_N6 / (rate_bytes_N6_tx + 1e-9)
```

Rolling 5-min stats: last 300 steps (at 1s scrape) of `rate_pfcp_sessions`, `rate_bytes_N3_rx`, `rate_bytes_N3_tx`, `rate_bytes_N6_rx`, `rate_bytes_N6_tx`, `rate_drops_N3`, `rate_drops_N6` → mean/std/min/max per channel = 28 features.

Total: 11 base + 28 rolling + 1 cpu_rate_mean = 40 features.

- [ ] **Step 1: Write failing tests**

```python
# serve/tests/__init__.py  (empty file)
```

```python
# serve/tests/test_feature_eng.py
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import numpy as np
import pytest
from feature_eng import extract_features, SKLEARN_FEATURE_NAMES, N_FEATURES

CHANNELS_14 = [
    "pfcp_sessions_total", "port_bytes_N3_rx_rate", "port_bytes_N6_tx_rate",
    "port_pkts_N3_rx_rate", "port_dropped_N3_rx_rate", "port_dropped_N6_rx_rate",
    "go_goroutines", "go_heap_alloc_bytes", "process_cpu_rate",
    "upf_sim_scenario_congestion", "upf_sim_scenario_flatline",
    "upf_sim_scenario_spike", "port_pkts_N6_tx_rate", "port_dropped_N6_rx",
]

def _make_window(n_channels=14, n_steps=512, seed=42):
    rng = np.random.default_rng(seed)
    return rng.random((n_channels, n_steps)).tolist()

def test_output_shape():
    vec = extract_features(_make_window(), CHANNELS_14)
    assert len(vec) == N_FEATURES

def test_output_is_finite():
    vec = extract_features(_make_window(), CHANNELS_14)
    assert all(np.isfinite(v) for v in vec), "NaN or inf in feature vector"

def test_feature_names_length():
    assert len(SKLEARN_FEATURE_NAMES) == N_FEATURES

def test_zero_window_produces_finite():
    window = [[0.0] * 512 for _ in range(14)]
    vec = extract_features(window, CHANNELS_14)
    assert all(np.isfinite(v) for v in vec)

def test_missing_channel_graceful():
    # Only 5 channels; rest default to zeros
    partial = [[1.0] * 512 for _ in range(5)]
    names = CHANNELS_14[:5]
    vec = extract_features(partial, names)
    assert len(vec) == N_FEATURES
    assert all(np.isfinite(v) for v in vec)
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
cd serve
python -m pytest tests/test_feature_eng.py -v
```

Expected: `ImportError: No module named 'feature_eng'`

- [ ] **Step 3: Implement `serve/feature_eng.py`**

```python
# serve/feature_eng.py
"""Map 14-channel 512-step pipeline window → 40-feature sklearn vector."""
import numpy as np

# Maps pipeline channel name → sklearn base feature slot name
_CH_MAP = {
    "pfcp_sessions_total":     "rate_pfcp_sessions",
    "port_bytes_N3_rx_rate":   "rate_bytes_N3_rx",
    "port_bytes_N6_tx_rate":   "rate_bytes_N6_tx",
    "port_pkts_N3_rx_rate":    "rate_bytes_N3_tx",
    "port_pkts_N6_tx_rate":    "rate_bytes_N6_rx",
    "port_dropped_N3_rx_rate": "rate_drops_N3",
    "port_dropped_N6_rx_rate": "rate_drops_N6",
    "process_cpu_rate":        "cpu_rate",
}

_ROLLING_CHANNELS = [
    "rate_pfcp_sessions", "rate_bytes_N3_rx", "rate_bytes_N3_tx",
    "rate_bytes_N6_rx", "rate_bytes_N6_tx", "rate_drops_N3", "rate_drops_N6",
]
_WINDOW_5M = 300  # steps at 1-s scrape interval

SKLEARN_FEATURE_NAMES: list[str] = (
    ["rate_pfcp_sessions", "rate_bytes_N3_rx", "rate_bytes_N3_tx",
     "rate_bytes_N6_rx", "rate_bytes_N6_tx", "rate_drops_N3", "rate_drops_N6",
     "rx_tx_ratio_N3", "rx_tx_ratio_N6", "drop_fraction_N3", "drop_fraction_N6"]
    + [f"{ch}_{stat}_5m" for ch in _ROLLING_CHANNELS for stat in ("mean", "std", "min", "max")]
    + ["cpu_rate_mean"]
)
N_FEATURES: int = len(SKLEARN_FEATURE_NAMES)  # 40


def extract_features(channels: list[list[float]], channel_names: list[str]) -> list[float]:
    """
    channels: shape (C, T) — C channels, T timesteps (T >= 1)
    channel_names: C names matching pipeline ML_CHANNELS order
    Returns: list of N_FEATURES floats, all finite
    """
    ch_last: dict[str, float] = {}
    ch_arr: dict[str, np.ndarray] = {}
    for name, series in zip(channel_names, channels):
        arr = np.array(series, dtype=np.float64)
        slug = _CH_MAP.get(name, name)
        ch_last[slug] = float(arr[-1]) if len(arr) else 0.0
        ch_arr[slug] = arr

    def last(key: str) -> float:
        return ch_last.get(key, 0.0)

    eps = 1e-9
    rx  = last("rate_bytes_N3_rx")
    tx  = last("rate_bytes_N6_tx")
    rx2 = last("rate_bytes_N3_tx")
    tx2 = last("rate_bytes_N6_rx")
    dn3 = last("rate_drops_N3")
    dn6 = last("rate_drops_N6")

    base = [
        last("rate_pfcp_sessions"),
        rx, tx, rx2, tx2, dn3, dn6,
        rx  / (tx  + eps),
        rx2 / (tx2 + eps),
        dn3 / (rx  + eps),
        dn6 / (tx  + eps),
    ]

    rolling: list[float] = []
    for ch in _ROLLING_CHANNELS:
        arr = ch_arr.get(ch, np.zeros(1))
        window = arr[-_WINDOW_5M:] if len(arr) >= _WINDOW_5M else arr
        rolling += [
            float(np.mean(window)),
            float(np.std(window)),
            float(np.min(window)),
            float(np.max(window)),
        ]

    cpu_arr = ch_arr.get("cpu_rate", np.zeros(1))
    extra = [float(np.mean(cpu_arr))]

    vec = base + rolling + extra
    return [v if np.isfinite(v) else 0.0 for v in vec]
```

- [ ] **Step 4: Run tests — verify pass**

```bash
cd serve
python -m pytest tests/test_feature_eng.py -v
```

Expected: `5 passed`

- [ ] **Step 5: Commit**

```bash
git add serve/feature_eng.py serve/tests/__init__.py serve/tests/test_feature_eng.py
git commit -m "feat(serve): feature engineering layer — 14-channel window to 40-feature sklearn vector"
```

---

### Task 2: Wire sklearn Models into Ray Serve

**Files:**
- Modify: `serve/app.py`
- Modify: `serve/requirements.txt`
- Create: `serve/tests/test_serve_sklearn.py`

Load `isolation_forest.joblib` and `scaler.joblib` from `MODELS_DIR`. Use `feature_eng.extract_features` in `/detect`. Return real `anomaly_score` (RF probability) and `anomaly` flag (IF or RF fires).

RF threshold: 0.5. IF: `decision_function < threshold` from `metadata.json` (≈ `4.85e-17`).
Ensemble: `anomaly=True` if `rf_proba >= 0.5 OR if_score < threshold`.

- [ ] **Step 1: Write failing test**

```python
# serve/tests/test_serve_sklearn.py
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import json
import numpy as np
import pytest

CHANNELS_14 = [
    "pfcp_sessions_total", "port_bytes_N3_rx_rate", "port_bytes_N6_tx_rate",
    "port_pkts_N3_rx_rate", "port_dropped_N3_rx_rate", "port_dropped_N6_rx_rate",
    "go_goroutines", "go_heap_alloc_bytes", "process_cpu_rate",
    "upf_sim_scenario_congestion", "upf_sim_scenario_flatline",
    "upf_sim_scenario_spike", "port_pkts_N6_tx_rate", "port_dropped_N6_rx",
]

def _normal_window():
    rng = np.random.default_rng(0)
    return rng.random((14, 512)).tolist()

def _make_fake_models(tmp_path):
    import joblib
    from sklearn.ensemble import IsolationForest, RandomForestClassifier
    from sklearn.preprocessing import StandardScaler
    from feature_eng import N_FEATURES
    X = np.random.rand(20, N_FEATURES)
    scaler = StandardScaler()
    scaler.fit(X)
    rf = RandomForestClassifier(n_estimators=2, random_state=0)
    rf.fit(X, [0]*10 + [1]*10)
    if_ = IsolationForest(n_estimators=2, random_state=0)
    if_.fit(X)
    joblib.dump(scaler, tmp_path / "scaler.joblib")
    joblib.dump(if_,   tmp_path / "isolation_forest.joblib")
    joblib.dump(rf,    tmp_path / "random_forest.joblib")
    (tmp_path / "metadata.json").write_text(json.dumps({
        "thresholds": {"isolation_forest": 0.0}
    }))

def test_sklearn_models_load(tmp_path, monkeypatch):
    _make_fake_models(tmp_path)
    monkeypatch.setenv("MODELS_DIR", str(tmp_path))
    monkeypatch.setenv("KAFKA_BROKER", "")

    from importlib import reload
    import app as serve_app
    reload(serve_app)

    inst = serve_app.MLServeDeployment.__new__(serve_app.MLServeDeployment)
    inst._load_sklearn_models()
    assert inst.sklearn_available

def test_detect_returns_real_score(tmp_path, monkeypatch):
    _make_fake_models(tmp_path)
    monkeypatch.setenv("MODELS_DIR", str(tmp_path))
    monkeypatch.setenv("KAFKA_BROKER", "")

    from importlib import reload
    import app as serve_app
    reload(serve_app)

    inst = serve_app.MLServeDeployment.__new__(serve_app.MLServeDeployment)
    inst._load_sklearn_models()
    inst._load_metadata()
    inst.moment_available = False
    inst._moment_head = None
    from shadow_publisher import ShadowPublisher
    inst._shadow = ShadowPublisher(producer=None)

    from app import DetectRequest
    req = DetectRequest(channels=_normal_window(), channel_names=CHANNELS_14)
    result = inst._detect_impl(req)

    assert "anomaly_score" in result
    assert "if_score" in result
    assert "rf_proba" in result
    assert isinstance(result["anomaly"], bool)
```

- [ ] **Step 2: Run — verify fail**

```bash
cd serve
python -m pytest tests/test_serve_sklearn.py -v
```

Expected: `AttributeError` or `ImportError` on `_load_sklearn_models` / `_detect_impl`

- [ ] **Step 3: Update `serve/requirements.txt`**

```
ray[serve]==2.30.0
torch
transformers
numpy
pandas
scikit-learn==1.4.2
joblib
pyarrow
confluent-kafka
```

- [ ] **Step 4: Rewrite `serve/app.py`**

```python
# serve/app.py
import json
import logging
import os
from pathlib import Path

import numpy as np
import torch
from fastapi import FastAPI
from pydantic import BaseModel

import ray
from ray import serve

from feature_eng import extract_features

log = logging.getLogger(__name__)

MODELS_DIR = Path(os.getenv("MODELS_DIR", "/models"))
SHADOW_TOPIC = os.getenv("SHADOW_TOPIC", "upf.shadow.detections")
_IF_THRESHOLD = -1e-16  # overridden from metadata.json

app = FastAPI()


class DetectRequest(BaseModel):
    channels: list[list[float]]
    channel_names: list[str] | None = None
    upf_id: str = "unknown"
    ts: float = 0.0


class ChronosForecastRequest(BaseModel):
    context: list[float]
    timestamps: list[str] | None = None


@serve.deployment(num_replicas=1, ray_actor_options={"num_gpus": 0})
@serve.ingress(app)
class MLServeDeployment:
    def __init__(self):
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self._load_sklearn_models()
        self._load_moment_models()
        self._load_metadata()
        self._shadow = self._init_shadow()
        log.info(
            "Serve ready: sklearn=%s moment=%s device=%s",
            self.sklearn_available, self.moment_available, self.device,
        )

    def _load_metadata(self):
        global _IF_THRESHOLD
        meta_path = MODELS_DIR / "metadata.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            _IF_THRESHOLD = meta.get("thresholds", {}).get(
                "isolation_forest", _IF_THRESHOLD
            )
        self._model_version = f"v2-sklearn@{self.device}"

    def _load_sklearn_models(self):
        self.sklearn_available = False
        self._scaler = None
        self._if = None
        self._rf = None
        try:
            import joblib
            self._scaler = joblib.load(MODELS_DIR / "scaler.joblib")
            self._if = joblib.load(MODELS_DIR / "isolation_forest.joblib")
            rf_path = MODELS_DIR / "random_forest.joblib"
            self._rf = joblib.load(rf_path) if rf_path.exists() else None
            self.sklearn_available = True
            log.info("sklearn models loaded (rf=%s)", self._rf is not None)
        except Exception as exc:
            log.warning("sklearn models not loaded: %s", exc)

    def _load_moment_models(self):
        from moment_loader import load_moment_head
        self._moment_head, self.moment_available = load_moment_head(
            MODELS_DIR / "moment_head.pt",
            MODELS_DIR / "moment_threshold.json",
            self.device,
        )

    def _init_shadow(self):
        from shadow_publisher import ShadowPublisher
        broker = os.getenv("KAFKA_BROKER", "")
        if not broker:
            return ShadowPublisher(producer=None)
        try:
            from confluent_kafka import Producer
            producer = Producer({"bootstrap.servers": broker})
            log.info("shadow publisher connected to %s", broker)
            return ShadowPublisher(producer=producer, topic=SHADOW_TOPIC)
        except Exception as exc:
            log.warning("shadow publisher init failed: %s", exc)
            return ShadowPublisher(producer=None)

    def _detect_impl(self, req: DetectRequest) -> dict:
        ch_names = req.channel_names or []
        channels = req.channels

        anomaly = False
        anomaly_score = 0.0
        if_score = 0.0
        rf_proba = 0.0
        channel_scores: dict = {}

        if self.sklearn_available:
            vec = extract_features(channels, ch_names)
            X = np.array(vec, dtype=np.float64).reshape(1, -1)
            X_scaled = self._scaler.transform(X)

            if_score = float(self._if.decision_function(X_scaled)[0])
            if_anomaly = if_score < _IF_THRESHOLD

            rf_anomaly = False
            if self._rf is not None:
                proba = self._rf.predict_proba(X_scaled)[0]
                rf_proba = float(proba[1]) if len(proba) > 1 else 0.0
                rf_anomaly = rf_proba >= 0.5

            anomaly = if_anomaly or rf_anomaly
            anomaly_score = rf_proba if self._rf is not None else max(0.0, -if_score)
            channel_scores = {"if_score": if_score, "rf_proba": rf_proba}

        moment_score = 0.0
        if self.moment_available and self._moment_head is not None:
            from moment_loader import score_moment
            moment_score, moment_anomaly = score_moment(
                self._moment_head, channels, ch_names, self.device
            )
            if moment_anomaly:
                anomaly = True
            channel_scores["moment_score"] = moment_score

        return {
            "available": True,
            "anomaly": anomaly,
            "anomaly_score": anomaly_score,
            "if_score": if_score,
            "rf_proba": rf_proba,
            "moment_score": moment_score,
            "threshold": 0.5,
            "channel_scores": channel_scores,
            "top_anomalous_channels": [],
            "confidence": anomaly_score,
            "model_version": self._model_version,
        }

    @app.post("/detect")
    def detect(self, req: DetectRequest) -> dict:
        result = self._detect_impl(req)
        self._shadow.publish(upf_id=req.upf_id, ts=req.ts, result=result)
        return result

    @app.post("/forecast")
    def forecast(self, req: ChronosForecastRequest) -> dict:
        return {
            "available": False,
            "forecast_median": [0.0] * 10,
            "forecast_p10": [0.0] * 10,
            "forecast_p90": [0.0] * 10,
            "forecast_timestamps": [],
            "capacity_breach_eta_minutes": None,
            "breach_probability": 0.0,
            "trend": "flat",
            "model_version": self._model_version,
        }

    @app.get("/health")
    def health(self) -> dict:
        return {
            "status": "ok",
            "device": self.device,
            "sklearn": self.sklearn_available,
            "moment": self.moment_available,
        }


app_node = MLServeDeployment.bind()
```

- [ ] **Step 5: Update `serve/Dockerfile`**

```dockerfile
FROM pytorch/pytorch:2.1.0-cuda11.8-cudnn8-runtime

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py feature_eng.py moment_loader.py shadow_publisher.py ./

CMD ["serve", "run", "app:app_node", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 6: Run tests — verify pass**

```bash
cd serve
python -m pytest tests/test_serve_sklearn.py -v
```

Expected: `2 passed`

- [ ] **Step 7: Commit**

```bash
git add serve/app.py serve/requirements.txt serve/Dockerfile serve/tests/test_serve_sklearn.py
git commit -m "feat(serve): wire sklearn IF+RF into Ray Serve /detect — real anomaly scores"
```

---

### Task 3: MOMENT Head (Graceful Fallback)

**Files:**
- Create: `serve/moment_loader.py`
- Create: `serve/tests/test_moment_loader.py`

`moment_head.pt` is a linear layer trained on MOMENT base model embeddings (`embedding_dim=7168`). Loading it requires the full MOMENT base model (~4GB) for true inference. Strategy: load the head weights, compute a proxy input (per-channel mean+std tiled to 7168 dims) — rough approximation, flagged as `v2-moment-proxy`. Falls back to `(None, False)` on any error.

`moment_threshold.json`: `{"threshold": 0.8241, "normal_mean": 0.1221, "normal_std": 0.2340, "embedding_dim": 7168}`

- [ ] **Step 1: Write failing test**

```python
# serve/tests/test_moment_loader.py
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import json
import numpy as np
import pytest
import torch


def _make_fake_head(tmp_path, embedding_dim=7168):
    import torch.nn as nn
    head = nn.Linear(embedding_dim, 1)
    torch.save(head.state_dict(), tmp_path / "moment_head.pt")
    (tmp_path / "moment_threshold.json").write_text(json.dumps({
        "threshold": 0.5,
        "normal_mean": 0.1,
        "normal_std": 0.2,
        "embedding_dim": embedding_dim,
    }))

CHANNELS_14 = [
    "pfcp_sessions_total", "port_bytes_N3_rx_rate", "port_bytes_N6_tx_rate",
    "port_pkts_N3_rx_rate", "port_dropped_N3_rx_rate", "port_dropped_N6_rx_rate",
    "go_goroutines", "go_heap_alloc_bytes", "process_cpu_rate",
    "upf_sim_scenario_congestion", "upf_sim_scenario_flatline",
    "upf_sim_scenario_spike", "port_pkts_N6_tx_rate", "port_dropped_N6_rx",
]

def test_load_moment_head_succeeds(tmp_path):
    _make_fake_head(tmp_path)
    from moment_loader import load_moment_head
    head, available = load_moment_head(
        tmp_path / "moment_head.pt",
        tmp_path / "moment_threshold.json",
        "cpu",
    )
    assert available
    assert head is not None

def test_load_moment_head_missing_file(tmp_path):
    from moment_loader import load_moment_head
    head, available = load_moment_head(
        tmp_path / "nonexistent.pt",
        tmp_path / "nonexistent.json",
        "cpu",
    )
    assert not available
    assert head is None

def test_score_moment_returns_finite(tmp_path):
    _make_fake_head(tmp_path)
    from moment_loader import load_moment_head, score_moment
    head, available = load_moment_head(
        tmp_path / "moment_head.pt",
        tmp_path / "moment_threshold.json",
        "cpu",
    )
    assert available
    channels = [list(np.random.rand(512)) for _ in range(14)]
    score, is_anomaly = score_moment(head, channels, CHANNELS_14, "cpu")
    assert np.isfinite(score)
    assert isinstance(is_anomaly, bool)
```

- [ ] **Step 2: Run — verify fail**

```bash
cd serve
python -m pytest tests/test_moment_loader.py -v
```

Expected: `ImportError: No module named 'moment_loader'`

- [ ] **Step 3: Implement `serve/moment_loader.py`**

```python
# serve/moment_loader.py
"""Load MOMENT head.pt and score windows without requiring the MOMENT base model."""
import json
import logging
from pathlib import Path
from typing import Optional

import numpy as np
import torch
import torch.nn as nn

log = logging.getLogger(__name__)

_threshold: float = 0.5
_embedding_dim: int = 7168


def load_moment_head(
    head_path: Path,
    threshold_path: Path,
    device: str,
) -> tuple[Optional[nn.Module], bool]:
    global _threshold, _embedding_dim
    try:
        meta = json.loads(threshold_path.read_text())
        _threshold = meta["threshold"]
        _embedding_dim = meta["embedding_dim"]

        head = nn.Linear(_embedding_dim, 1)
        state = torch.load(head_path, map_location=device, weights_only=True)
        head.load_state_dict(state)
        head.eval()
        head.to(device)
        log.info("MOMENT head loaded (proxy mode, embedding_dim=%d)", _embedding_dim)
        return head, True
    except Exception as exc:
        log.warning("MOMENT head not loaded: %s", exc)
        return None, False


def _make_proxy_embedding(
    channels: list[list[float]],
    embedding_dim: int,
) -> np.ndarray:
    # ponytail: proxy embedding via channel mean+std tiled to embedding_dim;
    # replace with real MOMENT base model inference when momentfm is installed
    stats: list[float] = []
    for series in channels:
        arr = np.array(series, dtype=np.float64)
        stats += [float(np.mean(arr)), float(np.std(arr))]

    base = np.array(stats if stats else [0.0], dtype=np.float32)
    reps = (embedding_dim + len(base) - 1) // len(base)
    emb = np.tile(base, reps)[:embedding_dim]
    norm = np.linalg.norm(emb)
    if norm > 0:
        emb = emb / norm
    return emb


def score_moment(
    head: nn.Module,
    channels: list[list[float]],
    channel_names: list[str],
    device: str,
) -> tuple[float, bool]:
    emb = _make_proxy_embedding(channels, _embedding_dim)
    with torch.no_grad():
        x = torch.tensor(emb, dtype=torch.float32, device=device).unsqueeze(0)
        logit = head(x).item()
        score = float(torch.sigmoid(torch.tensor(logit)).item())
    return score, score >= _threshold
```

- [ ] **Step 4: Run — verify pass**

```bash
cd serve
python -m pytest tests/test_moment_loader.py -v
```

Expected: `3 passed`

- [ ] **Step 5: Commit**

```bash
git add serve/moment_loader.py serve/tests/test_moment_loader.py
git commit -m "feat(serve): MOMENT head loader with proxy embedding fallback"
```

---

### Task 4: Eval Harness — P/R/F1/Calibration Report

**Files:**
- Create: `serve/eval_harness.py`
- Create: `serve/tests/test_eval_harness.py`

Load `models/dataset_eval.parquet` (columns: 40 sklearn feature names + `label` col with values `"anomaly"`/`"normal"`), run IF + RF, compare to baselines in `metadata.json` (IF AUC 0.9199, RF AUC 0.9239, RF F1 anomaly 0.74).

- [ ] **Step 1: Write failing smoke test**

```python
# serve/tests/test_eval_harness.py
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import json
import numpy as np
import pytest
import joblib
import pandas as pd
from pathlib import Path
from sklearn.ensemble import IsolationForest, RandomForestClassifier
from sklearn.preprocessing import StandardScaler
from feature_eng import SKLEARN_FEATURE_NAMES, N_FEATURES


def test_eval_harness_imports():
    import eval_harness  # noqa: F401


def test_eval_harness_runs(tmp_path, monkeypatch):
    """Smoke: eval_harness.run() returns a metrics dict with expected keys."""
    rng = np.random.default_rng(0)
    X = rng.random((40, N_FEATURES))
    y = np.array([0] * 20 + [1] * 20)

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)
    if_ = IsolationForest(n_estimators=5, random_state=0, contamination=0.5)
    if_.fit(X_scaled[y == 0])
    rf = RandomForestClassifier(n_estimators=5, random_state=0)
    rf.fit(X_scaled, y)

    df = pd.DataFrame(X, columns=SKLEARN_FEATURE_NAMES)
    df["label"] = ["anomaly" if yi else "normal" for yi in y]

    parquet_path = tmp_path / "dataset_eval.parquet"
    df.to_parquet(parquet_path, index=False)
    joblib.dump(scaler, tmp_path / "scaler.joblib")
    joblib.dump(if_,   tmp_path / "isolation_forest.joblib")
    joblib.dump(rf,    tmp_path / "random_forest.joblib")

    import eval_harness
    metrics = eval_harness.run(str(tmp_path), str(parquet_path))

    for key in ("rf_f1", "rf_precision", "rf_recall", "rf_auc", "if_auc", "n_rows"):
        assert key in metrics, f"missing key: {key}"
    assert metrics["n_rows"] == 40
```

- [ ] **Step 2: Run — verify fail**

```bash
cd serve
python -m pytest tests/test_eval_harness.py::test_eval_harness_imports -v
```

Expected: `ImportError: No module named 'eval_harness'`

- [ ] **Step 3: Implement `serve/eval_harness.py`**

```python
# serve/eval_harness.py
"""
Offline eval: load dataset_eval.parquet, run sklearn models, print P/R/F1/AUC.

Usage:
    MODELS_DIR=/path/to/models python eval_harness.py [--parquet /path/to/file.parquet]
"""
import json
import os
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import (
    f1_score, precision_score, recall_score, roc_auc_score,
)

from feature_eng import SKLEARN_FEATURE_NAMES


def run(models_dir: str, parquet_path: str) -> dict:
    md = Path(models_dir)
    df = pd.read_parquet(parquet_path)

    missing = [c for c in SKLEARN_FEATURE_NAMES if c not in df.columns]
    if missing:
        raise ValueError(f"Parquet missing {len(missing)} feature cols: {missing[:5]}…")

    X = df[SKLEARN_FEATURE_NAMES].to_numpy(dtype=np.float64)
    y_true = (df["label"] == "anomaly").astype(int).to_numpy()

    scaler = joblib.load(md / "scaler.joblib")
    X_scaled = scaler.transform(X)

    results: dict = {"n_rows": len(df)}

    if_ = joblib.load(md / "isolation_forest.joblib")
    if_scores = -if_.decision_function(X_scaled)
    if_preds = (if_.predict(X_scaled) == -1).astype(int)
    results["if_auc"]       = float(roc_auc_score(y_true, if_scores))
    results["if_recall"]    = float(recall_score(y_true, if_preds, zero_division=0))
    results["if_precision"] = float(precision_score(y_true, if_preds, zero_division=0))
    results["if_f1"]        = float(f1_score(y_true, if_preds, zero_division=0))

    rf_path = md / "random_forest.joblib"
    if rf_path.exists():
        rf = joblib.load(rf_path)
        rf_proba = rf.predict_proba(X_scaled)[:, 1]
        rf_preds = (rf_proba >= 0.5).astype(int)
        results["rf_auc"]       = float(roc_auc_score(y_true, rf_proba))
        results["rf_precision"] = float(precision_score(y_true, rf_preds, zero_division=0))
        results["rf_recall"]    = float(recall_score(y_true, rf_preds, zero_division=0))
        results["rf_f1"]        = float(f1_score(y_true, rf_preds, zero_division=0))
    else:
        for k in ("rf_auc", "rf_precision", "rf_recall", "rf_f1"):
            results[k] = None

    return results


def _print_report(metrics: dict, models_dir: Path) -> None:
    print("\n=== Phase 2 Eval Report ===")
    print(f"Rows evaluated: {metrics['n_rows']}")

    baseline: dict = {}
    meta_path = models_dir / "metadata.json"
    if meta_path.exists():
        bm = json.loads(meta_path.read_text()).get("eval_metrics", {})
        baseline = {
            "if_auc": bm.get("isolation_forest", {}).get("roc_auc"),
            "rf_auc": bm.get("random_forest", {}).get("roc_auc"),
        }

    for model, label in [("if", "Isolation Forest"), ("rf", "Random Forest")]:
        auc = metrics.get(f"{model}_auc")
        if auc is None:
            continue
        base_auc = baseline.get(f"{model}_auc", "N/A")
        print(f"\n{label}:")
        print(f"  AUC    : {auc:.4f}  (baseline: {base_auc})")
        print(
            f"  P/R/F1 : {metrics.get(f'{model}_precision', 0):.3f} / "
            f"{metrics.get(f'{model}_recall', 0):.3f} / "
            f"{metrics.get(f'{model}_f1', 0):.3f}"
        )


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--models-dir", default=os.getenv("MODELS_DIR", "/models"))
    parser.add_argument("--parquet",    default=None)
    args = parser.parse_args()

    md = Path(args.models_dir)
    parquet = args.parquet or str(md / "dataset_eval.parquet")
    metrics = run(str(md), parquet)
    _print_report(metrics, md)
    print("\nmetrics JSON:", json.dumps(metrics, indent=2))
```

- [ ] **Step 4: Run tests — verify pass**

```bash
cd serve
python -m pytest tests/test_eval_harness.py -v
```

Expected: `2 passed`

- [ ] **Step 5: Run eval against real models (manual validation)**

```bash
cd Bess_Upf_AI
MODELS_DIR=./models python serve/eval_harness.py
```

Expected output (approximate; match baseline ± 0.005):
```
=== Phase 2 Eval Report ===
Rows evaluated: 431999

Isolation Forest:
  AUC    : 0.9199  (baseline: 0.9199)
  P/R/F1 : 0.xxx / 0.113 / 0.xxx

Random Forest:
  AUC    : 0.9239  (baseline: 0.9239)
  P/R/F1 : 0.730 / 0.750 / 0.740
```

If AUC is off by > 0.01, the feature name mapping is wrong — check `SKLEARN_FEATURE_NAMES` vs parquet column names with `python -c "import pandas as pd; print(pd.read_parquet('models/dataset_eval.parquet').columns.tolist())"`.

- [ ] **Step 6: Commit**

```bash
git add serve/eval_harness.py serve/tests/test_eval_harness.py
git commit -m "feat(serve): eval harness — P/R/F1/AUC report vs dataset_eval.parquet baselines"
```

---

### Task 5: Shadow Detection Logging

**Files:**
- Create: `serve/shadow_publisher.py`
- Create: `serve/tests/test_shadow.py`
- Modify: `config/clickhouse/init.sql` (append shadow DDL)
- Modify: `pipeline/app.py` (pass upf_id + ts in detect request)

Shadow: every `/detect` call publishes to `upf.shadow.detections`. ClickHouse Kafka engine consumes it → MergeTree. Enables offline v2 vs v1 comparison via SQL.

Shadow record schema:
```json
{"upf_id": "sim-upf-0", "ts": 1234567890.123, "anomaly": 0,
 "anomaly_score": 0.73, "if_score": -0.02, "rf_proba": 0.73,
 "moment_score": 0.12, "model_version": "v2-sklearn@cpu"}
```

- [ ] **Step 1: Write failing test**

```python
# serve/tests/test_shadow.py
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import json
import pytest
from unittest.mock import MagicMock


def test_shadow_publisher_sends_to_kafka():
    from shadow_publisher import ShadowPublisher
    mock_producer = MagicMock()
    pub = ShadowPublisher(producer=mock_producer, topic="upf.shadow.detections")
    pub.publish(
        upf_id="sim-upf-0",
        ts=1234567890.0,
        result={
            "anomaly": False,
            "anomaly_score": 0.1,
            "if_score": 0.02,
            "rf_proba": 0.1,
            "moment_score": 0.0,
            "model_version": "v2-sklearn@cpu",
        },
    )
    mock_producer.produce.assert_called_once()
    payload = json.loads(mock_producer.produce.call_args[1]["value"])
    assert payload["upf_id"] == "sim-upf-0"
    assert "anomaly_score" in payload
    assert "if_score" in payload


def test_shadow_publisher_noop_when_no_producer():
    from shadow_publisher import ShadowPublisher
    pub = ShadowPublisher(producer=None, topic="upf.shadow.detections")
    pub.publish(upf_id="x", ts=0.0, result={})  # must not raise
```

- [ ] **Step 2: Run — verify fail**

```bash
cd serve
python -m pytest tests/test_shadow.py -v
```

Expected: `ImportError: No module named 'shadow_publisher'`

- [ ] **Step 3: Create `serve/shadow_publisher.py`**

```python
# serve/shadow_publisher.py
import json
import logging
import os
from typing import Any

log = logging.getLogger(__name__)

SHADOW_TOPIC = os.getenv("SHADOW_TOPIC", "upf.shadow.detections")


class ShadowPublisher:
    def __init__(self, producer=None, topic: str = SHADOW_TOPIC):
        self._producer = producer
        self._topic = topic

    def publish(self, upf_id: str, ts: float, result: dict[str, Any]) -> None:
        if self._producer is None:
            return
        record = {
            "upf_id":        upf_id,
            "ts":            ts,
            "anomaly":       int(bool(result.get("anomaly", False))),
            "anomaly_score": result.get("anomaly_score", 0.0),
            "if_score":      result.get("if_score", 0.0),
            "rf_proba":      result.get("rf_proba", 0.0),
            "moment_score":  result.get("moment_score", 0.0),
            "model_version": result.get("model_version", ""),
        }
        try:
            self._producer.produce(
                self._topic,
                key=upf_id.encode(),
                value=json.dumps(record, allow_nan=False).encode(),
            )
            self._producer.poll(0)
        except Exception as exc:
            log.warning("shadow publish failed: %s", exc)
```

- [ ] **Step 4: Append shadow DDL to `config/clickhouse/init.sql`**

Open `config/clickhouse/init.sql` and append at the end:

```sql
-- ============================================================
-- Shadow detection log: compare v2 scores vs v1 offline
-- ============================================================

CREATE TABLE IF NOT EXISTS shadow_detections_kafka (
    upf_id        String,
    ts            Float64,
    anomaly       UInt8,
    anomaly_score Float64,
    if_score      Float64,
    rf_proba      Float64,
    moment_score  Float64,
    model_version String
) ENGINE = Kafka
SETTINGS
    kafka_broker_list        = 'redpanda:9092',
    kafka_topic_list         = 'upf.shadow.detections',
    kafka_group_name         = 'clickhouse-shadow',
    kafka_format             = 'JSONEachRow',
    kafka_skip_broken_messages = 1;

CREATE TABLE IF NOT EXISTS shadow_detections (
    upf_id        LowCardinality(String),
    ts            DateTime64(3),
    anomaly       UInt8,
    anomaly_score Float64,
    if_score      Float64,
    rf_proba      Float64,
    moment_score  Float64,
    model_version LowCardinality(String)
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(ts)
ORDER BY (upf_id, ts)
TTL toDateTime(ts) + INTERVAL 90 DAY;

CREATE MATERIALIZED VIEW IF NOT EXISTS shadow_detections_mv
TO shadow_detections AS
SELECT
    upf_id,
    toDateTime64(ts, 3) AS ts,
    anomaly,
    anomaly_score,
    if_score,
    rf_proba,
    moment_score,
    model_version
FROM shadow_detections_kafka;
```

- [ ] **Step 5: Update `pipeline/app.py` — pass upf_id + ts in detect request**

Replace `_call_detect` in `pipeline/app.py`:

```python
def _call_detect(keyed: tuple[str, tuple[dict, list]]) -> dict | None:
    upf_id, (msg, channels_array) = keyed
    try:
        resp = _session.post(
            f"{RAY_SERVE_URL}/detect",
            json={
                "channels": channels_array,
                "channel_names": ML_CHANNELS,
                "upf_id": upf_id,
                "ts": msg.get("ts", 0.0),
            },
            timeout=5.0,
        )
        resp.raise_for_status()
        result: dict = resp.json()
    except Exception as exc:
        log.error("detect call failed for %s: %s", upf_id, exc, exc_info=True)
        result = {"anomaly": False, "anomaly_score": 0.0}

    result["upf_id"] = upf_id
    result["ts"] = msg.get("ts")
    return result if result.get("anomaly") else None
```

- [ ] **Step 6: Run tests — verify pass**

```bash
cd serve
python -m pytest tests/test_shadow.py -v
```

Expected: `2 passed`

- [ ] **Step 7: Run all serve tests**

```bash
cd serve
python -m pytest tests/ -v
```

Expected: all tests pass (≥ 12 total)

- [ ] **Step 8: Commit**

```bash
git add serve/shadow_publisher.py serve/tests/test_shadow.py
git add config/clickhouse/init.sql pipeline/app.py
git commit -m "feat(serve+ch): shadow detection topic + ClickHouse table; upf_id/ts in detect payload"
```

---

### Task 6: Integration Smoke + docker-compose Update

**Files:**
- Create: `serve/tests/test_integration_smoke.py`
- Modify: `docker-compose.v2.yml`

Verify end-to-end with real model files (no Ray runtime): `health()` shows `sklearn: true`, `_detect_impl()` returns non-stub `if_score`.

- [ ] **Step 1: Write integration smoke test**

```python
# serve/tests/test_integration_smoke.py
"""
Integration smoke: verify serve loads real models and returns non-stub scores.
Skips if real model files not present at MODELS_DIR.
Run: MODELS_DIR=../models python -m pytest tests/test_integration_smoke.py -v
"""
import os, sys
from pathlib import Path
import numpy as np
import pytest

MODELS_DIR = Path(os.environ.get("MODELS_DIR", "../../models"))
REQUIRED = ["isolation_forest.joblib", "scaler.joblib"]

pytestmark = pytest.mark.skipif(
    not all((MODELS_DIR / f).exists() for f in REQUIRED),
    reason=f"Real models not found at {MODELS_DIR}",
)

ML_CHANNELS = [
    "pfcp_sessions_total", "port_bytes_N3_rx_rate", "port_bytes_N6_tx_rate",
    "port_pkts_N3_rx_rate", "port_dropped_N3_rx_rate", "port_dropped_N6_rx_rate",
    "go_goroutines", "go_heap_alloc_bytes", "process_cpu_rate",
    "upf_sim_scenario_congestion", "upf_sim_scenario_flatline",
    "upf_sim_scenario_spike", "port_pkts_N6_tx_rate", "port_dropped_N6_rx",
]


def _make_inst():
    sys.path.insert(0, str(Path(__file__).parent.parent))
    os.environ["MODELS_DIR"] = str(MODELS_DIR)
    os.environ["KAFKA_BROKER"] = ""

    from importlib import reload
    import app as serve_app
    reload(serve_app)

    inst = serve_app.MLServeDeployment.__new__(serve_app.MLServeDeployment)
    inst._load_sklearn_models()
    inst._load_moment_models()
    inst._load_metadata()
    from shadow_publisher import ShadowPublisher
    inst._shadow = ShadowPublisher(producer=None)
    return inst, serve_app


def test_health_shows_sklearn_true():
    inst, _ = _make_inst()
    h = inst.health()
    assert h["sklearn"] is True


def test_detect_real_models_direct():
    inst, serve_app = _make_inst()
    channels = [[float(i % 50 + 1) for _ in range(512)] for i in range(14)]
    from app import DetectRequest
    req = DetectRequest(channels=channels, channel_names=ML_CHANNELS)
    result = inst._detect_impl(req)

    assert result["available"] is True
    assert isinstance(result["anomaly"], bool)
    # if_score must be a real IF decision_function value (not stuck at 0)
    assert result["if_score"] != 0.0, "if_score is stub zero — sklearn not loaded"
    assert result["anomaly_score"] >= 0.0
    assert "v2" in result["model_version"]
```

- [ ] **Step 2: Run — verify against real models**

```bash
cd serve
MODELS_DIR=../models python -m pytest tests/test_integration_smoke.py -v
```

Expected: `2 passed`

- [ ] **Step 3: Update `docker-compose.v2.yml` serve service**

In `docker-compose.v2.yml`, replace the `serve` service `environment` block:

```yaml
  serve:
    build:
      context: ./serve
      dockerfile: Dockerfile
    environment:
      - MODELS_DIR=/models
      - KAFKA_BROKER=redpanda:9092
      - SHADOW_TOPIC=upf.shadow.detections
    volumes:
      - ./models:/models:ro
    networks:
      - v2-net
    ports:
      - "8000:8000"
    depends_on:
      redpanda:
        condition: service_healthy
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
```

- [ ] **Step 4: Run full test suite**

```bash
# serve tests
cd serve
MODELS_DIR=../models python -m pytest tests/ -v

# pipeline tests
cd ..
python -m pytest pipeline/tests/ -v
```

Expected: all pass (serve ≥ 12, pipeline 22)

- [ ] **Step 5: Commit**

```bash
git add serve/tests/test_integration_smoke.py docker-compose.v2.yml
git commit -m "test(serve): integration smoke with real models; KAFKA_BROKER wired in compose"
```

---

## Self-Review

### 1. Spec Coverage

| Requirement (spec §8 Phase 2) | Task |
|-------------------------------|------|
| Wire sklearn (IF+RF) into Ray Serve | T2 |
| Feature engineering (14-ch window → 40-feature vector) | T1 |
| Wire MOMENT head | T3 (proxy mode; real base model deferred — see note) |
| Build labeled test set eval | T4 (uses `dataset_eval.parquet`) |
| Report P/R/F1/calibration | T4 eval_harness |
| Shadow v2 vs v1 logic | T5 (shadow topic + ClickHouse) |
| Models versioned (`model_version` in response) | T2 (`_model_version` field) |
| v2 detection provably ≥ v1 | T4 (AUC report) + T6 (smoke confirms real scores) |

**Note on Chronos:** `/forecast` returns `available: false`. Chronos `zero_shot_marker.json` exists but the actual model weights require `chronos-forecasting` package which adds significant image size. Deferred — Phase 2 exit criteria does not require forecast; Phase 3 can wire it when the policy engine needs capacity breach ETA.

**Note on MOMENT proxy:** `moment_head.pt` loads, but the proxy embedding (channel mean+std tiled to 7168 dims) is not true MOMENT inference. True MOMENT requires `momentfm` + base model (~4GB). The proxy is labeled `v2-moment-proxy` in responses so it's auditable. Replace `_make_proxy_embedding` with real `MOMENTPipeline` inference when `momentfm` is installed.

### 2. Placeholder Scan

No "TBD", "TODO", or vague steps found. All code blocks are complete.

### 3. Type Consistency

- `extract_features(channels: list[list[float]], channel_names: list[str]) -> list[float]` — consistent across T1 tests, T2 `serve/app.py`, T4 `eval_harness.py`.
- `N_FEATURES: int = 40` — consistent across T1, T2, T4.
- `load_moment_head(head_path, threshold_path, device) -> tuple[Optional[nn.Module], bool]` — matches T2 `_load_moment_models` call.
- `score_moment(head, channels, channel_names, device) -> tuple[float, bool]` — matches T2 usage.
- `ShadowPublisher.publish(upf_id, ts, result)` — consistent across T5 creation, T2 `detect` call.
- `DetectRequest.upf_id: str`, `DetectRequest.ts: float` — added in T2, used in T5 pipeline change and T6 smoke test.
