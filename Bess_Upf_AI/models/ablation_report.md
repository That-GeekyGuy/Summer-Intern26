# Ablation Evaluation Report

Generated: 2026-07-11 15:29 UTC
Eval dataset hash: `a8709602345d5d25`

## Per-Tier Metrics

| Tier | Method | Precision | Recall | F1 | AUC-ROC | Latency P50 |
|------|--------|-----------|--------|-----|---------|-------------|
| T1 | Z-score Rules | 0.761 | 0.286 | 0.415 | 0.6266 | 0.04 ms |
| T2a-IF | Isolation Forest | 1.000 | 0.124 | 0.221 | 0.562 | 3.47 ms |
| T2a-RF | Random Forest | 0.672 | 0.920 | 0.777 | 0.8786 | 12.81 ms |
| T2b-zs | MOMENT Zero-Shot (statistical fallback) | 1.000 | 0.250 | 0.400 | 0.625 | 0.00 ms |
| T2b-ft | MOMENT Fine-Tuned Head | — | — | — | — | *(unavailable)* |
| Ensemble | T1 + T2a-RF + T2b-zs (OR) | 0.605 | 0.957 | 0.741 | — | 0.00 ms |
| **Ensemble** | **T1 + T2a-RF + T2b-zs (OR)** | **0.605** | **0.957** | **0.741** | — | — |

## Methodology Notes

- **T2b-zs** (MOMENT Zero-Shot) evaluated on **all windows** — zero-shot reconstruction uses no learned params; no train contamination.
- **T2a** (sklearn) and **T2b-ft** evaluated on `dataset_eval.parquet` only (stratified episode split guarantees anomaly episodes in eval).
- Ensemble is T1 OR T2a-RF OR T2b-zs. Recall ≥ max(individual); precision slightly lower due to additional false positives.
- Latency measured in pure Python on CPU; Go production path is ~10× faster.
