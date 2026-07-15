# Phase 4c: Platform SLOs

This document defines the Service Level Objectives (SLOs) for the CoreWatch v2 single-path streaming platform. These guarantees act as the contract between the platform team and the operators relying on its closed-loop mitigations.

## Core SLOs

### 1. Control Loop Latency
- **Objective**: 99% of `observe → detect → decide → act` cycles must complete in **< 1 second**.
- **Measurement**: Measured via distributed tracing across Kafka (ingestion time), Bytewax (window emission time), Ray Serve (inference time), and the Policy Engine (action decision time).
- **Rationale**: The moat of the product relies on mitigating critical anomalies before they escalate. Sub-second latency is required for automated scaling or flow-rerouting to be effective in 5G workloads.

### 2. Analytics & Audit Freshness
- **Objective**: 99% of anomaly events and mitigation actions must land in ClickHouse (and be available to dashboards/LLM) in **< 5 seconds**.
- **Measurement**: Difference between Kafka event timestamp and ClickHouse insertion timestamp.
- **Rationale**: The control loop runs entirely on the hot path (in-memory/Kafka) and never waits for ClickHouse. The 5-second freshness guarantees operators have near real-time visibility into what the AI just did.

### 3. Availability
- **Objective**: 99.9% uptime for the Bytewax pipeline, Ray Serve inference endpoints, and the Mitigation policy engine.
- **Measurement**: Blackbox probing via `ingress-nginx` health endpoints and continuous simulated load tests.
- **Rationale**: If the AI goes down, the network loses its self-healing capability, falling back to manual operational overhead.

### 4. Safety Constraints
- **Objective**: 0% violation of configured Blast-Radius Caps and Trust Ladder permissions.
- **Measurement**: ClickHouse `action_audit` queries counting actions triggered vs actions permitted, as well as dry-run logging vs active-run logging.
- **Rationale**: An autonomous system's credibility relies on its safety guarantees. Exceeding a blast radius cap (e.g., terminating too many sessions) is treated as a critical severity outage.

## Monitoring Strategy

Since Phase 0 explicitly removed Prometheus and VictoriaMetrics to enforce a single-path design, platform meta-monitoring will be performed natively using ClickHouse:
- All services (Bytewax, Serve, Mitigation) emit their internal telemetry directly to Kafka `platform.metrics` topics.
- A ClickHouse Kafka engine table consumes this topic and materializes it for dashboarding.
- Alerts will be configured via Grafana (which now points solely to ClickHouse) to notify operators if latency or availability SLOs are breached.
