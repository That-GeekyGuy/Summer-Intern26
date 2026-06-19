#!/usr/bin/env python3
"""
Tier 2 model training script.

Trains two models on the dataset produced by prepare_dataset.py:
  Model A — Isolation Forest (unsupervised, trained on normal rows only)
  Model B — Random Forest classifier (supervised, binary: normal vs anomaly)

Uses a time-based 80/20 train/eval split (no random shuffle — temporal data must
not be split randomly to avoid leakage).

Outputs (all written to /models/):
  isolation_forest.joblib   — trained IF model
  random_forest.joblib      — trained RF model
  scaler.joblib             — StandardScaler (fitted on training set)
  scaler_params.json        — mean + scale per feature (for Go inference without Python)
  metadata.json             — model card: thresholds, eval metrics, training time
  training_report.md        — human-readable evaluation report

Usage:
    docker run --rm -v ./models:/models bess-ml-tools python train/train.py [--detection-db /path/to/anomalies.db]
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest, RandomForestClassifier
from sklearn.metrics import (
    classification_report,
    confusion_matrix,
    roc_auc_score,
)
from sklearn.preprocessing import StandardScaler

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

MODELS_DIR = Path(os.getenv("MODELS_DIR", "/models"))
IF_FP_RATE  = float(os.getenv("IF_FP_RATE", "0.01"))   # target false-positive rate for IF threshold
RF_N_TREES  = int(os.getenv("RF_N_TREES", "100"))
TRAIN_RATIO = 0.80


def _load_dataset() -> pd.DataFrame:
    path = MODELS_DIR / "dataset.parquet"
    if not path.exists():
        log.error("dataset.parquet not found — run prepare_dataset.py first")
        sys.exit(1)
    df = pd.read_parquet(str(path))
    feature_path = MODELS_DIR / "feature_columns.json"
    if not feature_path.exists():
        log.error("feature_columns.json not found — run prepare_dataset.py first")
        sys.exit(1)
    feature_cols = json.loads(feature_path.read_text())
    return df, feature_cols


def _time_split(df: pd.DataFrame, ratio: float):
    """Split by time (first ratio fraction = train, rest = eval). No shuffle."""
    df = df.sort_values("timestamp").reset_index(drop=True)
    split_idx = int(len(df) * ratio)
    return df.iloc[:split_idx].copy(), df.iloc[split_idx:].copy()


def _maybe_load_tier1_events(db_path: str | None) -> dict[str, list]:
    """
    Optionally load Tier 1 events from the detection service SQLite for earliness comparison.
    Returns a dict mapping scenario label → list of first-fire Unix timestamps.
    """
    if not db_path:
        return {}
    try:
        import sqlite3
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        cur.execute(
            "SELECT rule_name, MIN(timestamp) as first_fire FROM anomaly_events "
            "WHERE event_type = 'reactive' GROUP BY rule_name"
        )
        rows = cur.fetchall()
        con.close()
        return {row[0]: row[1] for row in rows}
    except Exception as e:
        log.warning("could not load Tier 1 events from %s: %s", db_path, e)
        return {}


def train(detection_db: str | None = None):
    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    df, FEATURE_COLS = _load_dataset()
    log.info("loaded %d rows, %d features", len(df), len(FEATURE_COLS))

    # ── Filter transition rows ────────────────────────────────────────────────
    df = df[df["label"] != "transition"].copy()
    log.info("after dropping transitions: %d rows", len(df))

    label_counts = df["label"].value_counts().to_dict()
    warnings = []
    for lbl, cnt in label_counts.items():
        if lbl != "normal" and cnt < 100:
            warnings.append(f"Scenario '{lbl}' has only {cnt} rows — model is unreliable for this class")
            log.warning("⚠ %s", warnings[-1])

    # ── Time split ────────────────────────────────────────────────────────────
    df_train, df_eval = _time_split(df, TRAIN_RATIO)
    log.info("train: %d rows  eval: %d rows", len(df_train), len(df_eval))

    # Verify all scenarios appear in eval (flag if not)
    for lbl in label_counts:
        if lbl != "normal" and lbl not in df_eval["label"].values:
            warnings.append(f"Scenario '{lbl}' does not appear in eval set (only one window in dataset)")
            log.warning("⚠ %s", warnings[-1])

    X_train = df_train[FEATURE_COLS].values.astype(float)
    X_eval  = df_eval[FEATURE_COLS].values.astype(float)
    y_train_raw = df_train["label"].values
    y_eval_raw  = df_eval["label"].values

    # Binary labels: 0=normal, 1=anomaly
    y_train_bin = (y_train_raw != "normal").astype(int)
    y_eval_bin  = (y_eval_raw  != "normal").astype(int)

    # ── StandardScaler (fit on all training data) ─────────────────────────────
    scaler = StandardScaler()
    # Impute NaN with column mean before fitting
    X_train_clean = np.where(np.isnan(X_train), np.nanmean(X_train, axis=0), X_train)
    X_eval_clean  = np.where(np.isnan(X_eval),  np.nanmean(X_train, axis=0), X_eval)

    scaler.fit(X_train_clean)
    X_train_sc = scaler.transform(X_train_clean)
    X_eval_sc  = scaler.transform(X_eval_clean)

    # Export scaler params as JSON so Go can apply scaling without Python
    scaler_params = {
        "feature_names": FEATURE_COLS,
        "mean":  scaler.mean_.tolist(),
        "scale": scaler.scale_.tolist(),
    }
    (MODELS_DIR / "scaler_params.json").write_text(json.dumps(scaler_params, indent=2))
    # joblib (pickle-based) is safe here: output is written only to the local models/ volume,
    # consumed only by our own inference sidecar, never from an untrusted source.
    joblib.dump(scaler, str(MODELS_DIR / "scaler.joblib"))
    log.info("scaler saved")

    # ── Model A: Isolation Forest ─────────────────────────────────────────────
    X_train_normal_sc = X_train_sc[y_train_bin == 0]
    log.info("training IF on %d normal rows", len(X_train_normal_sc))
    clf_if = IsolationForest(contamination=IF_FP_RATE, random_state=42, n_jobs=-1)
    clf_if.fit(X_train_normal_sc)

    # Threshold at IF_FP_RATE percentile of normal training scores
    normal_scores = clf_if.decision_function(X_train_normal_sc)
    threshold_if = float(np.percentile(normal_scores, IF_FP_RATE * 100))
    log.info("IF decision threshold (%.0f%% FP): %.4f", IF_FP_RATE * 100, threshold_if)

    # Eval IF
    eval_scores_if = clf_if.decision_function(X_eval_sc)
    y_pred_if = (eval_scores_if < threshold_if).astype(int)
    eval_fp_if = float(np.mean(y_pred_if[y_eval_bin == 0]))
    eval_recall_if = float(np.mean(y_pred_if[y_eval_bin == 1])) if y_eval_bin.sum() > 0 else 0.0
    try:
        roc_if = roc_auc_score(y_eval_bin, -eval_scores_if)
    except Exception:
        roc_if = float("nan")

    joblib.dump(clf_if, str(MODELS_DIR / "isolation_forest.joblib"))
    log.info("IF trained: FP=%.2f%%  recall=%.2f%%  ROC-AUC=%.3f",
             eval_fp_if * 100, eval_recall_if * 100, roc_if)

    # ── Model B: Random Forest classifier ────────────────────────────────────
    log.info("training RF on %d rows (%d anomaly, %d normal)",
             len(X_train_sc), y_train_bin.sum(), (y_train_bin == 0).sum())
    clf_rf = RandomForestClassifier(n_estimators=RF_N_TREES, random_state=42, n_jobs=-1)
    clf_rf.fit(X_train_sc, y_train_bin)

    y_pred_rf   = clf_rf.predict(X_eval_sc)
    y_proba_rf  = clf_rf.predict_proba(X_eval_sc)[:, 1]
    try:
        roc_rf = roc_auc_score(y_eval_bin, y_proba_rf)
    except Exception:
        roc_rf = float("nan")

    rf_report = classification_report(y_eval_bin, y_pred_rf, target_names=["normal", "anomaly"])
    rf_cm = confusion_matrix(y_eval_bin, y_pred_rf).tolist()

    # Feature importances (top 10 for report)
    feat_importance = sorted(
        zip(FEATURE_COLS, clf_rf.feature_importances_),
        key=lambda x: x[1], reverse=True,
    )

    joblib.dump(clf_rf, str(MODELS_DIR / "random_forest.joblib"))
    log.info("RF trained: ROC-AUC=%.3f", roc_rf)

    # ── Earliness analysis ────────────────────────────────────────────────────
    # For each scenario window in eval, find seconds from start to first detection
    tier1_events = _maybe_load_tier1_events(detection_db)
    earliness: dict[str, dict] = {}

    scenarios_in_eval = df_eval[df_eval["label"] != "normal"]["label"].unique()
    for scenario in scenarios_in_eval:
        mask = df_eval["label"].values == scenario
        idxs = np.where(mask)[0]
        if len(idxs) == 0:
            continue
        scenario_start_ts = df_eval["timestamp"].iloc[idxs[0]]

        # First IF fire in this window
        fired_if = np.where(mask & (eval_scores_if < threshold_if))[0]
        first_if_ts = df_eval["timestamp"].iloc[fired_if[0]] if len(fired_if) > 0 else None

        # First RF fire in this window
        fired_rf = np.where(mask & (y_pred_rf == 1))[0]
        first_rf_ts = df_eval["timestamp"].iloc[fired_rf[0]] if len(fired_rf) > 0 else None

        earliness[scenario] = {
            "start_ts": int(scenario_start_ts),
            "if_seconds_after_start":  int(first_if_ts - scenario_start_ts) if first_if_ts else None,
            "rf_seconds_after_start":  int(first_rf_ts - scenario_start_ts) if first_rf_ts else None,
        }

    # ── Metadata / model card ────────────────────────────────────────────────
    metadata = {
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "dataset_time_range": {
            "start": int(df["timestamp"].min()),
            "end":   int(df["timestamp"].max()),
        },
        "label_counts": {k: int(v) for k, v in label_counts.items()},
        "eval_metrics": {
            "isolation_forest": {
                "roc_auc":            round(roc_if, 4),
                "false_positive_rate": round(eval_fp_if, 4),
                "recall_anomaly":      round(eval_recall_if, 4),
            },
            "random_forest": {
                "roc_auc": round(roc_rf, 4),
            },
        },
        "thresholds": {
            "isolation_forest": threshold_if,
            "random_forest":    0.5,
        },
        "feature_names": FEATURE_COLS,
        "earliness": earliness,
        "warnings": warnings,
    }
    (MODELS_DIR / "metadata.json").write_text(json.dumps(metadata, indent=2))
    log.info("metadata.json written")

    # ── Training report ──────────────────────────────────────────────────────
    top10_feats = "\n".join(
        f"  {i+1:2d}. {name:<40s} {imp:.4f}"
        for i, (name, imp) in enumerate(feat_importance[:10])
    )
    earliness_table = ""
    for sc, info in earliness.items():
        rf_s = f"{info['rf_seconds_after_start']}s" if info['rf_seconds_after_start'] is not None else "not detected"
        if_s = f"{info['if_seconds_after_start']}s" if info['if_seconds_after_start'] is not None else "not detected"
        earliness_table += f"  {sc:<25s}  RF: {rf_s:<12s}  IF: {if_s}\n"

    warning_section = ""
    if warnings:
        warning_section = "## ⚠ Warnings\n\n" + "\n".join(f"- {w}" for w in warnings) + "\n\n"

    report = f"""# Tier 2 ML Training Report

Generated: {metadata['trained_at']}

## Dataset

| Label | Rows |
|-------|------|
""" + "\n".join(
    f"| {k} | {v} |" for k, v in sorted(label_counts.items())
) + f"""

Time coverage: {pd.Timestamp(metadata['dataset_time_range']['start'], unit='s')} → {pd.Timestamp(metadata['dataset_time_range']['end'], unit='s')}

Train / eval split: {int(TRAIN_RATIO*100)}% / {int((1-TRAIN_RATIO)*100)}% (time-ordered, no shuffle)

{warning_section}## Model A: Isolation Forest

- Trained on **normal rows only** ({int(label_counts.get('normal', 0) * TRAIN_RATIO)} rows)
- Decision threshold: `{threshold_if:.4f}` (achieves ~{IF_FP_RATE*100:.0f}% false-positive rate on training normals)

| Metric | Value |
|--------|-------|
| ROC-AUC | {roc_if:.4f} |
| False-positive rate (eval) | {eval_fp_if*100:.2f}% |
| Recall anomaly (eval) | {eval_recall_if*100:.2f}% |

**Interpretation**: Model A caught {eval_recall_if*100:.1f}% of anomalies with {eval_fp_if*100:.1f}% false-positive rate.

## Model B: Random Forest Classifier

- Trained on all labeled rows (binary: normal vs anomaly)
- Default decision threshold: 0.5

```
{rf_report}
```

Confusion matrix (rows=actual, cols=predicted):
```
{confusion_matrix(y_eval_bin, y_pred_rf)}
```

ROC-AUC: **{roc_rf:.4f}**

### Top 10 Feature Importances

```
{top10_feats}
```

## Earliness: Seconds from Scenario Start to First Detection

{earliness_table if earliness_table else "_(No scenario windows found in eval set — increase dataset size)_"}

## Known Limitations

- **Tier 2 recognises the 6 trained scenario patterns only.** It will not reliably detect anomaly
  types it has not been trained on. Retrain as more data accumulates.
- Dataset was generated by the simulator. Real UPF traffic may differ from simulator behaviour.
- Dataset has < 24h total coverage. See README section "Path to general anomaly detection"
  for the 2–4 week accumulation plan.

## Path to General Anomaly Detection

Run the scenario generator continuously for 2–4 weeks (enough data for seasonal patterns).
With that corpus, replace the Random Forest with an Isolation Forest or autoencoder trained on
unlabeled normal traffic. Re-training is a config change: update `EXPORT_METRICS`, re-run
`prepare_dataset.py` → `train.py`, restart the inference sidecar.

Consider LLM-assisted labeling when > 2 weeks of real (non-simulated) traffic is available
with < 20% labeled windows.
"""
    report_path = MODELS_DIR / "training_report.md"
    report_path.write_text(report)
    log.info("training_report.md written to %s", report_path)

    log.info("training complete — restart the ml-infer service to pick up new models")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--detection-db", default=None,
        help="Path to detection service anomalies.db for Tier 1 earliness comparison"
    )
    args = parser.parse_args()
    train(detection_db=args.detection_db)
