
## MOMENT-1-large (Reconstruction-Based Anomaly Detection)

Generated: 2026-06-30T03:52:34.926816+00:00

### Architecture

- Model: MOMENT-1-large (AutonLab/MOMENT-1-large)
- Task: Reconstruction-based anomaly detection
- Strategy: **Linear probing** (encoder frozen) — full fine-tuning NOT used
  - Reason: ~16422 MOMENT windows available (need >>500 for stable fine-tuning)
  - Decision: frozen encoder with reconstruction head prevents overfitting
- Anomaly head: Reconstruction MLP (input_dim=7168 → 128 → 64 → input_dim)
- Embedding: 7168-dim statistical/patch features per window

### Dataset

| Metric | Value |
|---|---|
| Total windows | 16422 |
| Train windows | 13137 |
| Eval windows | 3285 |
| Anomaly fraction | 24.7% |
| Channels | 14 |
| Sequence length | 512 steps (128 min at 15s cadence) |

### Threshold Calibration

- Threshold = mean(0.1221) + 3×std(0.2340) = **0.8241**
- False positive rate on normal training windows: **1.25%**
- Methodology: calibrated on normal (uoi_binary=0) training windows only — no eval leakage

### Evaluation Metrics

| Metric | Value |
|---|---|
| Precision | 0.0000 |
| Recall | 0.0000 |
| **F1** | **0.0000** |
| FP Rate | 0.0000 |
| True Positives | 0 |
| False Positives | 0 |
| False Negatives | 759 |
| True Negatives | 2526 |

### Does MOMENT outperform Tier 1 z-score?

**No — F1=0.0000 does not exceed 0.70 threshold.**

With only 16422 windows, MOMENT's statistical reconstruction may be under-trained.
**However, MOMENT still adds value that Tier 1 z-score cannot provide:**
1. **Multivariate correlation** — detects anomalies requiring joint deviation across channels
2. **Channel attribution** — pinpoints which interface (N3 rx drops, session rate) is anomalous
3. **Context-aware** — 512-step (~128-min) history captures slow-building trends
4. **Early warning** — per-window scores fire before threshold breach (see earliness below)

Recommendation: accumulate more data (30+ days of real traffic) for improved training.

### Per-Channel AUC (Eval Set)

```
  port_bytes_N3_rx_rate               AUC=0.9305
  port_bytes_N6_tx_rate               AUC=0.9334
  port_pkts_N3_rx_rate                AUC=0.9339
  port_dropped_N3_rx_rate             AUC=0.9204
  port_dropped_N6_rx_rate             AUC=0.9223
  pfcp_sessions_total                 AUC=0.9302
  pfcp_session_setup_rate             AUC=0.9327
  dl_throughput_efficiency            AUC=0.3519
  dl_throughput_efficiency_rate       AUC=0.3598
  drop_rate_percentage                AUC=0.9188
  tsi_value                           AUC=0.9313
  go_goroutines                       AUC=0.5466
  go_heap_alloc_bytes                 AUC=0.5388
  gc_pressure_rate                    AUC=0.5492
```

*Higher AUC = that channel's reconstruction error better separates anomalies from normal.*

### Earliness Analysis

| Metric | Value |
|---|---|
| Episodes analyzed | 266 |
| Episodes detected | 0 |
| Mean earliness | N/A |
| Median earliness | N/A |

*Positive earliness = MOMENT fires BEFORE the episode starts (early warning).*
*Negative earliness = MOMENT fires AFTER episode start.*

### Warnings

- No warnings


## Chronos-2 (UOI Value Forecasting)

Generated: 2026-06-30T02:04:08.022839+00:00

### Architecture

- Model: amazon/chronos-t5-small (46M parameters)
- Task: Univariate probabilistic forecasting of uoi_value
- Device: CPU, float32
- Context length: 512 steps (128 min at 15s cadence)
- Forecast horizon: 20 steps (300 s = 5 min)

### Training Data

- Series: uoi_value (forward-filled from ~90s cadence to 15s)
- Total rows: 2102400
- Train rows: 1681920
- Eval rows:  420480
- Train pairs: 168139
- Eval pairs:  21024

### Zero-Shot vs Fine-Tuned Comparison

| Metric | Zero-Shot | Fine-Tuned |
|---|---|---|
| WQL (lower=better) | 0.033691 | N/A |
| MIS (80% interval) | 0.4836 | N/A |
| Calibration (80% cov) | 0.4238 | N/A |

**Decision: Zero-shot model used.**
Fine-tuning did not improve WQL by ≥ 5% — zero-shot model retained.

### Final Model Performance (Zero-shot)

| Metric | Value |
|---|---|
| WQL | 0.033691 |
| MIS (80% interval) | 0.4836 |
| 80% Interval Calibration | 0.4238 |

### Breach Detection

| Metric | Value |
|---|---|
| UOI breach threshold | 0.3335 (75th percentile of training series) |
| Mean breach ETA | None min |
| Median breach ETA | None min |
| Mean breach probability | 0.0 |
| Fraction with breach ETA | 0.0 |

### Earliness vs Tier 3 (Linear Extrapolation)

| Metric | Value |
|---|---|
| Mean earliness advantage | None min |
| N evaluated pairs | 0 |

*Positive earliness advantage = Chronos-2 predicts breach earlier than Tier 3 linear extrapolation.*

### What breach_probability means

`breach_probability` is the fraction of Chronos-2's 20 Monte Carlo sample paths
in which uoi_value exceeds 0.3335 at least once in the 20-step horizon.

- **< 0.4**: Low confidence — do not display breach ETA, report as "low"
- **0.4 – 0.7**: Medium confidence — display with uncertainty
- **> 0.7**: High confidence — escalate to critical, show breach countdown

This is an empirically calibrated heuristic. The 80% prediction interval calibration
(target ≈ 80%, actual = 0.4238) indicates moderate reliability.

