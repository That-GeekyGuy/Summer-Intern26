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
