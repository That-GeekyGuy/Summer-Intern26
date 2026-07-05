# BESS-UPF AI — Research-Grade System Design

**Date:** 2026-06-27  
**Status:** Approved (implicit — goal directive)  
**Branch:** feat/url-shortener  
**Authors:** Multi-role team review (CVO, System Architect, AI/ML Eng, Data Eng, Pipeline Eng, Frontend Dev, UX, Data Analyst, Security Eng, Manager)

---

## 1. Context and Research Contribution

BESS-UPF AI is an intelligent monitoring platform for 5G User Plane Functions (UPF). It ingests per-node telemetry (N3/N6 throughput, PFCP session rates, drop fractions) from a Prometheus → VictoriaMetrics pipeline, runs multi-tier anomaly detection, and generates LLM-based root cause analysis.

**Unique contribution (not in published literature):**  
First end-to-end evaluation of a *multi-scale foundation model ensemble* (MOMENT-1-large reconstruction + Chronos-2 prediction-interval breach) against classical and statistical baselines on 5G UPF telemetry, with calibrated uncertainty, LLM-generated RCA, and a live in-app ablation benchmark.

**Critical gap identified in current system:**  
Training report (2026-06-22) shows 0% anomaly recall on both sklearn models. Root cause: time-ordered 80/20 split placed all injected anomaly windows into the training set. All proposed ML improvements are blocked until this is fixed.

---

## 2. Sub-Project Decomposition

| ID | Sub-project | Blocks |
|----|-------------|--------|
| P1 | ML Pipeline Repair + Unified Evaluation Framework | P2, P4 |
| P2 | Research-Grade Frontend (benchmark panel, uncertainty viz) | paper figures |
| P3 | Security Hardening (LLM sanitisation, audit log, Aikido) | — |
| P4 | Playwright CI + Verification Gates | all PRs |

---

## 3. P1 — ML Pipeline Repair + Unified Evaluation

### 3.1 Architecture

```
simulator/real UPF
      │
      ▼
Prometheus (15s scrape)
      │ remote_write
      ▼
VictoriaMetrics
      │
      ├─── export-job (nightly) ─────────────────► MinIO Parquet
      │                                                │
      │                                     ┌──────────▼───────────┐
      │                                     │  prepare_dataset.py  │
      │                                     │  scenario-aware split│
      │                                     │  hash → version tag  │
      │                                     └──────────┬───────────┘
      │                                                │
      │                                     ┌──────────▼──────────┐
      │                                     │     train.py        │
      │                                     │  IF + RF (ensemble) │
      │                                     │  MOMENT head (FT)   │
      │                                     └──────────┬──────────┘
      │                                                │
      │                                     ┌──────────▼──────────┐
      │                                     │     eval.py         │
      │                                     │  F1/precision/recall│
      │                                     │  per tier + ablation│
      │                                     │  training_report.md │
      │                                     └──────────┬──────────┘
      │                                                │
      │                                          models/ (versioned)
      │
      ▼
detection service (Go :8081)
      │
      ├─── Tier 1: z-score, threshold, trend (rule-based)
      ├─── Tier 2a: sklearn IF+RF ensemble (ml-infer :8080)
      ├─── Tier 2b: MOMENT reconstruction error (moment-sidecar :8083)  ← PRIMARY
      ├─── Tier 2c: Chronos-2 P10/P90 breach (chronos-sidecar :8084)   ← UNCERTAINTY
      └─── Tier 3: OLS capacity forecast
```

### 3.2 Eval Split Fix (Critical)

**Problem:** `prepare_dataset.py` uses time-ordered split. Anomaly scenarios (injected for <1h windows) land entirely in train when dataset is <24h.

**Fix:** Scenario-aware stratified split.
- Parse `scenario_id` column from Parquet
- Group rows by scenario window
- Stratify: ensure each scenario type appears in both train and eval
- Minimum eval fraction: 20% of anomaly rows per scenario type
- Output: `dataset_train.parquet`, `dataset_eval.parquet`, `split_report.json`

### 3.3 MOMENT Zero-Shot Reconstruction (Tier 2b Primary)

MOMENT-1-large supports zero-shot anomaly detection via reconstruction error — no fine-tuning required. The existing `moment_head.pt` (fine-tuned classification head) can be kept as an ensemble vote but should no longer be the primary signal.

**Implementation:**
- Input: 8-channel sliding window (512 timesteps × 8 metrics)
- Output: per-channel reconstruction MSE, aggregate anomaly score
- Threshold: calibrated at 1% FPR on normal traffic (replaces current `0.0000` threshold)
- Advantage: generalises to unseen anomaly types without retraining

### 3.4 Chronos-2 Prediction Intervals (Tier 2c Uncertainty)

Chronos-2 outputs P10/P50/P90 per channel per horizon. An anomaly signal fires when:
- Observed value falls outside [P10 − σ, P90 + σ] for 3+ consecutive timesteps
- σ = calibrated slack from historical deviation

**New API endpoint on chronos-sidecar:** `POST /intervals` → `{channel, p10[], p50[], p90[], horizon}`

### 3.5 Ablation Evaluation Protocol

All tiers evaluated on the same held-out eval set:

| Tier | Method | Metrics |
|------|--------|---------|
| 1 | z-score + threshold | Precision, Recall, F1, Latency |
| 2a | sklearn IF + RF ensemble | Precision, Recall, F1, Latency |
| 2b | MOMENT reconstruction | Precision, Recall, F1, Latency |
| 2c | Chronos-2 P10/P90 | Precision, Recall, F1, Latency |
| 2d | 2b + 2c ensemble | Precision, Recall, F1, Latency |
| Full | Tier1 OR Tier2d | Precision, Recall, F1, Latency |

Results written to `models/ablation_report.json`:
```json
{
  "generated_at": "2026-06-27T10:00:00Z",
  "dataset_hash": "sha256:abc123",
  "tiers": [
    {
      "id": "tier1",
      "method": "z-score+threshold",
      "precision": 0.82,
      "recall": 0.61,
      "f1": 0.70,
      "latency_ms": 12
    }
  ]
}
```

### 3.6 Reproducible Pipeline

Every training run produces:
- `models/run_<hash>/` — dataset hash, model artifacts, eval report
- `models/latest/` → symlink to best run (by F1)
- `models/training_report.md` — regenerated automatically
- Git-committed `models/metadata.json` updated with run hash

---

## 4. P2 — Research-Grade Frontend

### 4.1 New Views

**`benchmark` view — Ablation Comparison Panel**
- Bar chart: F1/Precision/Recall per tier (from `ablation_report.json`)
- Latency scatter: detection latency (ms) vs F1 trade-off
- Replay selector: choose stored scenario, watch tier detections fire in real-time
- Export button: SVG download of each chart (paper figure quality)

**`forecast` view — Upgraded with Uncertainty Bands**
- Existing OLS forecast line
- Add Chronos-2 P10/P50/P90 ribbon (shaded area chart via Recharts)
- Show "breach zone" when observed crosses P10/P90

**`insights` view — Correlation Topology**
- Render `models/correlation_graph.json` as SVG force-directed graph (no new deps)
- Nodes: UPF metrics channels; edges: correlation coefficient
- Highlight anomalous channels in red during active anomaly

**Anomaly Feed — Confidence Breakdown**
- Add per-tier confidence bar to RCA panel (Tier 1 / 2a / 2b / 2c / LLM)
- Show MOMENT channel importance as ranked list (already partially implemented)

### 4.2 Design Constraints
- No new npm dependencies (Recharts already installed)
- Dark theme consistency (existing CSS variables)
- All new data via existing API client pattern (`src/api/client.ts`)
- New `benchmark` view added to AppShell routing and Sidebar navigation

---

## 5. P3 — Security Hardening

### 5.1 LLM Input Sanitisation

**Location:** `analysis/internal/llm/sanitise.go` (new file)

Rules applied before every LLM call:
1. Strip URLs matching `(https?|file|ftp|data):\/\/` — prevents SSRF via prompt injection
2. Max input length: 2000 chars (configurable via `LLM_MAX_INPUT_CHARS` env)
3. Block PromQL injection: reject inputs containing `{__name__=~` or bare metric selectors with duration outside quoted strings
4. Structured audit log entry: `{ts, user_hash, input_len, blocked_reason|null}`

### 5.2 PromQL Allowlist Fuzz

`go-fuzz` corpus targeting `analysis/internal/validator/` — seeds:
- Valid queries (pass)
- Label injection: `{job="upf", __name__=~".*secret.*"}`
- Time range injection: `[99999d]`
- Subquery abuse: `rate(x[1m])[1h:30s]`

### 5.3 Audit Log Schema

```json
{"ts":"2026-06-27T10:00:00Z","event":"llm_call","user_hash":"sha256:redacted","input_len":142,"promql_count":2,"blocked":false}
{"ts":"2026-06-27T10:00:01Z","event":"promql_blocked","reason":"label_injection","raw_fragment":"{__name__=~"}
```

### 5.4 Aikido Re-scan

Run `aikido_full_scan` after P3 implementation. Target: 0 new issues vs previous clean scan.

---

## 6. P4 — Playwright CI

### 6.1 Test Coverage

| Test | View | What it verifies |
|------|------|-----------------|
| `login.spec.ts` | Login | Form renders, invalid creds rejected |
| `overview.spec.ts` | Overview | Metric cards render, no console errors |
| `anomalies.spec.ts` | Anomaly Feed | Feed loads, RCA panel structure present |
| `forecast.spec.ts` | Forecast | Chart renders, uncertainty band present (post-P2) |
| `benchmark.spec.ts` | Benchmark | Ablation chart renders, all tiers shown (post-P2) |
| `chat.spec.ts` | Chat | Input field present, submit disabled when empty |
| `insights.spec.ts` | Insights | Correlation graph SVG renders (post-P2) |

### 6.2 CI Integration

- `playwright.config.ts` — base URL from `PLAYWRIGHT_BASE_URL` env var (default: `http://localhost:5173`)
- Tests in `frontend/e2e/` directory
- `docker compose up --wait` before tests in CI

---

## 7. Research Paper Alignment

**Title candidate:** "Multi-Scale Foundation Model Ensemble for Real-Time Anomaly Detection in 5G User Plane Functions"

**Sections mapped to implementation:**
- §3 System Architecture → existing README diagram (enhanced)
- §4 Detection Methods → P1 ablation protocol
- §5 Evaluation → `models/ablation_report.json`
- §6 Results → benchmark panel SVG export (P2)
- §7 Security Considerations → P3 threat model

**Baseline comparisons:**
- CUSUM (add as Tier 1 variant)
- Isolation Forest alone (current → fixed)
- MOMENT reconstruction alone
- Chronos-2 intervals alone
- **Proposed:** MOMENT + Chronos-2 ensemble (novel contribution)

---

## 8. Verification Gates

| Gate | Tool | Pass condition |
|------|------|----------------|
| Unit | `go test ./...` | 0 failures |
| ML eval | `eval.py` | F1 > 0.0 on held-out set |
| E2E | Playwright | All specs pass |
| Security | Aikido | 0 new critical/high |
| Lint | `golangci-lint`, `tsc --noEmit` | 0 errors |

---

## 9. Implementation Order

1. **P1.1** — Fix `prepare_dataset.py` eval split (scenario-aware stratification)
2. **P1.2** — Add Chronos-2 `/intervals` endpoint
3. **P1.3** — Wire MOMENT reconstruction as Tier 2b primary in detection service
4. **P1.4** — Ablation eval harness + `ablation_report.json`
5. **P3** — Security hardening (parallel with P1.4)
6. **P2** — Frontend benchmark panel + uncertainty bands + correlation graph
7. **P4** — Playwright test suite

---

*Spec self-review: No TBDs. No contradictions. Architecture matches feature descriptions. Scope bounded to improvements on existing system — no new infrastructure or npm packages required.*
