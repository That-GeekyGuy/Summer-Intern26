# BESS-UPF AI v2 — Single-Path Streaming Rebuild Design

**Date:** 2026-07-01
**Status:** Approved (design), pending implementation plan
**Author:** CTO/CVO audit session

---

## 1. Goal

Rebuild BESS-UPF AI as a **single-path, real-time streaming platform** that autonomously detects, explains, forecasts, and safely mitigates anomalies on 5G User Plane Functions. End goal is **carrier-grade production** deployment (tested on a small cluster, scalable to carrier scale).

Hard constraints from stakeholder:
- **Single processing path** (hot path only — no parallel warm/poll pipeline).
- **Single serving endpoint** — one store is the source of truth for all history, dashboards, LLM, training, audit.
- Open to breaking/rebuilding anything to hit the goal.

## 2. Product vision (self-healing 5G core)

Control loop, not a dashboard:

```
observe → detect → explain → forecast → decide → act → verify → learn
```

The moat is not the models (MOMENT/Chronos are public) — it is **safe closed-loop mitigation** plus the **operator-trust layer**: LLM explanations + human-in-loop approval + full audit + rollback. Autonomous remediation an operator can actually trust on a carrier network.

**Trust ladder (mandatory, per action class):** `observe → recommend → approve → auto`. Every actuator starts at `observe`. Promotion requires demonstrated precision on that action class. Safety mechanism and sales narrative both.

## 3. Architecture — hot path + ClickHouse single endpoint

```
UPF / upf-sim
   │
   ▼
Kafka (Redpanda)  topic: upf.metrics.raw   [schema registry, fail-loud on unmapped channels]
   │
   ▼
Bytewax stream processor  [keyed by upf_id+channel, checkpointed 512-step windows, exactly-once]
   │
   ├──────────────► Ray Serve  [MOMENT detect + Chronos forecast + sklearn, fractional GPU, versioned, canary/shadow]
   │                     │
   │                     ▼
   │              Kafka topic: upf.anomalies.critical  [window snapshot attached to event]
   │                     │
   │                     ▼
   │              Policy engine  [anomaly class + confidence + trust-level → action]
   │                     │
   │                     ▼
   │              Actuator catalog  [k8s HPA / XDP rate-limit / PFCP-N4 reroute; dry-run→approve→auto; rollback; blast-radius cap; rate-limit]
   │
   └──────────────► ClickHouse  ◄── SINGLE ENDPOINT / source of truth
                          ▲
        everything reads/writes here (seconds-fresh, not sub-second):
        • Grafana dashboards
        • analysis/LLM service (SQL queries, chat-over-history, RCA)
        • training-data export
        • immutable audit + compliance retention
```

**Two latency regimes, one path:**
- Control loop (detect → decide → act) runs entirely on Kafka + in-stream windowed state. Sub-second. Never touches ClickHouse.
- History / dashboards / LLM / training served by ClickHouse. Seconds-fresh is acceptable there.

ClickHouse replaces the entire v1 warm path (Prometheus + VictoriaMetrics + Grafana-over-VM + SQLite stores) with one analytical store. One ingestion path, one query language (SQL).

## 4. Components — keep / rebuild / drop

| Component | Decision | Notes |
|---|---|---|
| `upf-sim` (Go) | **Keep** | Carrier-grade simulator + scenario API, genuinely good |
| `analysis` LLM service (Go) | **Keep + retarget** | Keep orchestrator, tool-loop, RAG, audit, rate-limit. Rewrite PromQL validator → ClickHouse SQL validator. Bridge to anomaly topic for streaming RCA |
| `frontend` (React/TS) | **Keep + extend** | Add mitigation approval UI, live-stream view, action audit view |
| MOMENT / Chronos / sklearn models | **Keep, re-serve** | Move from FastAPI sidecars into Ray Serve, load for real, version them |
| `detection` (Go, Tier1/3) | **Fold into stream** | z-score/trend/threshold/OLS logic reimplemented in Bytewax processor |
| `export-job` (Go) | **Replace** | Training data now = query ClickHouse |
| Prometheus + VictoriaMetrics + Grafana-over-VM | **Drop** | Replaced by Kafka ingest + ClickHouse |
| SQLite anomaly + audit stores | **Drop** | Replaced by ClickHouse (HA, compliance) |
| `pipeline/app.py` (deque loop) | **Rebuild** | Replace with real Bytewax stateful processor |
| `serve/app.py` (mock Ray Serve) | **Rebuild** | Wire real models |
| `mitigation/app.py` (simulated) | **Rebuild** | Real policy engine + actuator catalog + guardrails |
| `scrape_to_kafka.py` (0.0 fallback) | **Rebuild** | Real exporter → Kafka with schema registry, fail-loud |

Language: hot path = Python (Bytewax + Ray, where ML lives). Operator brain (analysis/LLM) stays Go. Do not rewrite what works.

## 5. Safety design (closed-loop, non-negotiable)

- Every action class starts at `observe` (log only), promotes through `recommend` → `approve` (human gate) → `auto`.
- Confidence gate: no action below model-confidence threshold for that action class.
- Action rate-limiting + blast-radius cap (max N sessions/nodes affected per window).
- Dry-run mode for every actuator.
- Rollback path for every action.
- Immutable audit of every decision + action in ClickHouse (compliance).
- Real UPF actuators (PFCP/N4, vendor APIs) hidden behind the action-catalog interface — sim actuator and real actuator swappable.
- Platform meta-monitoring: the loop has its own SLOs and alerting.

## 6. ML credibility

- Labeled test set from `upf-sim` scenarios + real incidents.
- Report precision/recall/F1/calibration on THIS domain — not synthetic AUC.
- Shadow mode: v2 detection runs alongside old v1 logic, compared before cutover.
- Versioned models, canary rollout.
- Online feedback loop: operator confirm/dismiss → labeled data → drift detection → scheduled retrain → canary.

## 7. Carrier-grade hardening

- ClickHouse HA/replication; Kafka (Redpanda) replication; Ray cluster HA.
- Security: mTLS between services, RBAC, secrets manager, real certs, drop basic-auth-only.
- k8s + Helm + NetworkPolicies.
- Platform SLOs + meta-alerting + DR runbook.

## 8. Phased implementation

Strictly ordered 0 → 1 → 2 → 3 (each validates the next). Phases 4 and 5 parallelize once 3 lands.

- **Phase 0 — Stabilize & de-clutter.** Confirm a working baseline (fix v1 E2E metrics/LLM gap via systematic debugging). Delete junk (`analysis;C`, stray CSV dirs, root logs), pull 1.2GB artifact out of Git LFS into object store/registry, split 1010-line compose, stand up CI (build + tests + lint). Exit: clean repo, green CI, known-good baseline to shadow against.
- **Phase 1 — Real streaming backbone.** Kafka producer + schema registry, fail-loud on unmapped channels. Bytewax processor with keyed checkpointed windows replacing the deque loop. Topic contract tests. Stand up ClickHouse + sink from stream. Exit: real sim data flows sim→Kafka→windows→(mock) detect + lands durably in ClickHouse, no silent zeros.
- **Phase 2 — Real model serving + honest eval.** Wire MOMENT/Chronos/sklearn into Ray Serve. Build labeled test set; report precision/recall/F1/calibration. Shadow v2 detection vs v1 logic. Exit: v2 detection provably ≥ v1 on labeled set; models versioned.
- **Phase 3 — Safe closed-loop mitigation.** Policy engine + action catalog. All actions ship at `observe`. Dry-run, human-approval gate (RCA attached, surfaced in frontend), action rate-limit, blast-radius cap, rollback, immutable audit. Promote one low-risk action (HPA scale-up) `observe`→`approve`→`auto`, gated on shadow precision. Exit: one action class running auto in sim with full guardrails + audit.
- **Phase 4 — Carrier hardening.** ClickHouse/Kafka/Ray HA; mTLS/RBAC/secrets/real certs; k8s+Helm+NetworkPolicies; platform SLOs + DR. Exit: survives node loss on test cluster; security review passes.
- **Phase 5 — Learning loop.** Operator confirm/dismiss → labeled feedback → drift detection → scheduled retrain → canary. Exit: a dismissed false-positive measurably improves the next model.

## 9. Top risks

1. False-positive → self-inflicted outage. Mitigated only by trust ladder + confidence gate + action rate-limit.
2. Real UPF actuators are vendor-specific — largest integration unknown; keep behind action-catalog interface.
3. End-to-end loop latency SLA (detect sub-second, but actions have own latency) — define and monitor.
4. Skipping Phase 0 = building v2 with no trustworthy baseline to shadow against.
5. PromQL→SQL rewrite of analysis service — scoped work; must not regress the LLM validator's safety guarantees.

## 10. Explicitly out of scope (single-path mandate)

- No parallel Prometheus/VictoriaMetrics warm path.
- No PromQL (SQL over ClickHouse instead).
- Multi-region DR deferred to post-Phase-4.
