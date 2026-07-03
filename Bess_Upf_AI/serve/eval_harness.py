"""Eval harness: P/R/F1/AUC report for IF + RF models against dataset_eval.parquet."""
import argparse
import json
import os
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from sklearn.metrics import (
    precision_recall_fscore_support,
    roc_auc_score,
)

from feature_eng import SKLEARN_FEATURE_NAMES


def run(models_dir: str, parquet_path: str) -> dict:
    """Load models, score eval parquet, return metrics dict."""
    md = Path(models_dir)

    # ponytail: joblib/pickle — models_dir is our own artifact store, not user-supplied data
    scaler = joblib.load(md / "scaler.joblib")
    if_ = joblib.load(md / "isolation_forest.joblib")
    rf_path = md / "random_forest.joblib"
    rf = joblib.load(rf_path) if rf_path.exists() else None

    schema_cols = set(pq.read_schema(parquet_path).names)
    needed = SKLEARN_FEATURE_NAMES + ["label"]
    missing = [c for c in needed if c not in schema_cols]
    if missing:
        raise ValueError(f"Parquet missing {len(missing)} expected columns: {missing[:5]}")

    df = pd.read_parquet(parquet_path, columns=needed)
    X = df[SKLEARN_FEATURE_NAMES].to_numpy(dtype=np.float32)
    y = (df["label"] == "anomaly").astype(int).to_numpy()

    X_scaled = scaler.transform(X)

    if_scores = -if_.decision_function(X_scaled)  # higher = more anomalous
    if_preds = (if_.predict(X_scaled) == -1).astype(int)
    if_prec, if_rec, if_f1, _ = precision_recall_fscore_support(
        y, if_preds, pos_label=1, average="binary", zero_division=0
    )
    if_auc = roc_auc_score(y, if_scores)

    result: dict = {
        "n_rows": len(df),
        "if_auc": round(float(if_auc), 4),
        "if_precision": round(float(if_prec), 4),
        "if_recall": round(float(if_rec), 4),
        "if_f1": round(float(if_f1), 4),
        "rf_auc": None,
        "rf_precision": None,
        "rf_recall": None,
        "rf_f1": None,
    }

    if rf is not None:
        rf_proba = rf.predict_proba(X_scaled)[:, 1]
        rf_preds = (rf_proba >= 0.5).astype(int)
        rf_prec, rf_rec, rf_f1, _ = precision_recall_fscore_support(
            y, rf_preds, pos_label=1, average="binary", zero_division=0
        )
        rf_auc = roc_auc_score(y, rf_proba)
        result.update(
            rf_auc=round(float(rf_auc), 4),
            rf_precision=round(float(rf_prec), 4),
            rf_recall=round(float(rf_rec), 4),
            rf_f1=round(float(rf_f1), 4),
        )

    return result


def _print_report(metrics: dict, models_dir: str) -> None:
    meta_path = Path(models_dir) / "metadata.json"
    baselines: dict = {}
    if meta_path.exists():
        with open(meta_path) as f:
            baselines = json.load(f).get("eval_metrics", {})

    b_if = baselines.get("isolation_forest", {})
    b_rf = baselines.get("random_forest", {})

    def _delta(val, base_key, base_dict):
        base = base_dict.get(base_key)
        if val is None or base is None:
            return ""
        diff = val - base
        sign = "+" if diff >= 0 else ""
        return f"  (baseline {base:.4f}, delta {sign}{diff:.4f})"

    print("\n=== Eval Harness Report ===")
    print(f"rows evaluated : {metrics['n_rows']:,}")
    print()
    print("-- Isolation Forest --")
    print(f"  AUC       : {metrics['if_auc']:.4f}{_delta(metrics['if_auc'], 'roc_auc', b_if)}")
    print(f"  Precision : {metrics['if_precision']:.4f}")
    print(f"  Recall    : {metrics['if_recall']:.4f}{_delta(metrics['if_recall'], 'recall_anomaly', b_if)}")
    print(f"  F1        : {metrics['if_f1']:.4f}")
    print()
    print("-- Random Forest --")
    if metrics["rf_auc"] is None:
        print("  (random_forest.joblib not found — skipped)")
    else:
        print(f"  AUC       : {metrics['rf_auc']:.4f}{_delta(metrics['rf_auc'], 'roc_auc', b_rf)}")
        print(f"  Precision : {metrics['rf_precision']:.4f}")
        print(f"  Recall    : {metrics['rf_recall']:.4f}")
        print(f"  F1        : {metrics['rf_f1']:.4f}")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Eval harness for IF + RF anomaly models.")
    parser.add_argument("--models-dir", default=os.environ.get("MODELS_DIR", "/models"))
    parser.add_argument("--parquet", default=None)
    args = parser.parse_args()

    parquet = args.parquet or str(Path(args.models_dir) / "dataset_eval.parquet")
    metrics = run(args.models_dir, parquet)
    _print_report(metrics, args.models_dir)
    print(json.dumps(metrics, indent=2))
