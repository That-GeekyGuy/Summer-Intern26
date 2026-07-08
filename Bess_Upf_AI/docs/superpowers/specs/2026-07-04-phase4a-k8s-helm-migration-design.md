# Phase 4a: k8s/Helm Migration Baseline — Design

## Context

Phase 4 of the v2 rebuild roadmap (`docs/superpowers/specs/2026-07-01-bess-upf-v2-rebuild-design.md:120`) is "Carrier hardening": ClickHouse/Kafka/Ray HA, mTLS/RBAC/secrets/real certs, k8s+Helm+NetworkPolicies, platform SLOs + DR, exiting when the stack "survives node loss on test cluster; security review passes."

That bundles several largely independent subsystems (platform migration, HA, security hardening, DR). Rather than design all of it at once, Phase 4 is split into sub-phases, each with its own spec/plan/implementation cycle:

- **Phase 4a (this spec):** migrate the existing Docker Compose stack to Kubernetes via Helm, with the HA mechanisms needed to survive node loss for stateful services. No mTLS/RBAC/secrets-manager work here — that's Phase 4b.
- **Phase 4b (future):** security hardening — mTLS, RBAC, NetworkPolicies, secrets management, cert-manager for real certs.
- **Phase 4c (future):** DR — backup/restore runbooks, platform SLOs.

This sequencing was chosen because HA and k8s-native security tooling (cert-manager, NetworkPolicies) both assume a working k8s baseline exists first; building security hardening into the current Docker Compose stack would mean redoing it once the platform migrates anyway.

## Current State

The stack (`docker-compose.v2.yml`) runs as single-instance Docker Compose services on one machine:
- `redpanda` — single-broker Kafka-compatible queue
- `clickhouse` — single-node OLAP store (Kafka-engine tables → materialized views → MergeTree, per Phase 1/3 work)
- `serve` — Ray Serve, sklearn IF/RF + MOMENT proxy embedding model serving (Phase 2)
- `pipeline` — Bytewax stream processor (Phase 1)
- `mitigation` — the Phase 3 closed-loop mitigation engine (policy → guardrails → catalog → audit → approval)
- Caddy — TLS termination (self-signed dev certs via `scripts/gen-dev-certs.sh`) + routing (`/vm/*`, `/api/*`, `/grafana/*`, `/*`)

No k8s/Helm scaffolding exists yet. No container registry is in use (images are local-only). No HA — every service is a single point of failure.

## Goal

Get the same 6 logical services running on a local multi-node Kubernetes cluster via Helm, with the specific HA mechanisms needed to survive a node loss, and prove it with a repeatable node-kill test. This is a re-platform, not a rewrite — the pipeline logic (sim→Kafka→Bytewax windows→ClickHouse, serve inference, mitigation trust ladder) is unchanged; what changes is where it runs and how many replicas of each stateful piece exist.

## Architecture

**Cluster:** `kind`, 1 control-plane node + 3 worker nodes. Three workers is the minimum that lets a 3-replica StatefulSet lose one node and keep 2 replicas (quorum-preserving) live.

**Namespace:** single `bess-upf` namespace. No per-service namespace split in this phase — namespace-scoped RBAC boundaries are Phase 4b's concern, not needed yet.

**Images & registry:** each service's existing Dockerfile is built and pushed to GHCR (`ghcr.io/that-geekyguy/bess-upf-<service>:<tag>`). The cluster pulls via a k8s `Secret` of type `kubernetes.io/dockerconfigjson` (GHCR PAT, `packages:read` scope) referenced as `imagePullSecrets` on each pod spec. A plain k8s Secret is acceptable for this baseline; encryption-at-rest / external secrets management is explicitly Phase 4b scope, not this one.

**Helm structure:** one umbrella chart at `charts/bess-upf/`, with `Chart.yaml` declaring dependencies on 6 subcharts: `redpanda`, `clickhouse`, `serve`, `pipeline`, `mitigation`, and `ingress-nginx` (community chart, not hand-rolled). A single `helm install bess-upf ./charts/bess-upf -n bess-upf --create-namespace` brings up the whole stack; a single `helm uninstall` tears it down. Shared config (image registry, tag, namespace) lives in the umbrella chart's `values.yaml` and is passed down to subcharts.

### Per-service HA design

**redpanda** — StatefulSet, 3 replicas, topics created with replication factor 3. `PodDisruptionBudget` with `maxUnavailable: 1` so a voluntary disruption (rolling update, node drain) can't take out enough brokers to lose quorum. This directly answers "Kafka HA" from the Phase 4 exit criteria via Redpanda's own multi-broker replication — no external storage layer needed for durability, because the app layer already replicates data across replicas' independent volumes.

**clickhouse** — StatefulSet, 3 replicas, with ClickHouse Keeper running as a 3-node quorum (can colocate as sidecars on the same 3 pods) for coordinating replicated tables. The existing `action_audit` and `shadow_detections` table definitions (`config/clickhouse/init.sql`) change their engine from `MergeTree`/`Kafka` to `ReplicatedMergeTree` variants so each replica holds a full copy and stays in sync via Keeper. Losing one node loses one replica's local disk, but the other two still serve reads/writes and the lost replica resyncs from its peers once rescheduled.

**serve (Ray)** — deployed via the KubeRay operator, which manages a `RayCluster` custom resource (1 head pod + N worker pods, N=2 to start). The head pod's GCS (global control store — Ray's cluster metadata) is backed by an external Redis `Deployment` (single replica; Redis here is just a metadata cache for GCS recovery, not itself the subject of the HA test). If the node hosting the head pod is lost, k8s reschedules the head pod and it recovers cluster state from Redis instead of restarting cold — this is Ray's documented production HA pattern (GCS fault tolerance), not a workaround.

**pipeline (Bytewax) & mitigation** — plain Deployments, 2 replicas each. Neither holds data that needs replication at this layer: Bytewax's exactly-once semantics come from its own checkpointing into Kafka/ClickHouse (established in Phase 1), and mitigation's state (guardrails' in-memory rate/blast-radius windows, approval store) is process-local and acceptable to lose on a pod restart per the existing design — 2 replicas here is purely for availability during node loss/rolling updates, not data durability.

**ingress-nginx** — community Helm chart, `Service` type `NodePort` (kind clusters don't get a real cloud `LoadBalancer` without extra tooling like `cloud-provider-kind`, and NodePort is sufficient for a local test cluster). The existing Caddyfile's routing rules (`/vm/*` → victoriametrics, `/api/*` → analysis, `/grafana/*` → grafana, `/*` → frontend) are translated path-for-path into `Ingress` resources. Caddy itself is retired in this phase (not deployed as a pod) — nginx-ingress takes over both roles (routing + eventual TLS termination point), which avoids doing ingress twice when Phase 4b adds cert-manager/mTLS on top of it.

## Data Flow

Unchanged from Phase 0–3 at the logical level: sim → Kafka (Redpanda) → Bytewax windowing → ClickHouse ingestion, `serve` consuming windowed features for inference, `mitigation` consuming `upf.anomalies.critical` through the trust-ladder → guardrails → catalog → audit pipeline built in Phase 3. This phase changes deployment topology and replica counts, not message formats, topic names, or table schemas (aside from the `MergeTree` → `ReplicatedMergeTree` engine change needed for replica coordination).

## Verification

This phase's entire point is proving "survives node loss," so verification is the deliverable, not an afterthought:

1. `helm install bess-upf ./charts/bess-upf -n bess-upf --create-namespace`; wait for all pods `Ready`.
2. Run the existing pytest suites (`mitigation/tests/`, `pipeline/tests/`, `serve/tests/` or equivalent) against the in-cluster services (via `kubectl port-forward` or the NodePort ingress) to confirm functional parity with the Compose baseline — no regressions from the platform change alone.
3. **Node-loss test**, repeated per stateful component:
   - Write a canary record (e.g., a synthetic anomaly event) and confirm it lands in ClickHouse.
   - `docker stop kind-worker<N>` for the node hosting a target replica (identify via `kubectl get pods -o wide`).
   - Confirm: (a) the remaining replicas keep serving reads/writes without interruption, (b) k8s marks the lost pod `NotReady` and reschedules it onto a surviving node, (c) once rescheduled, the replica resyncs and rejoins quorum (ClickHouse via Keeper, Redpanda via its own replication protocol), (d) the canary record is still readable after recovery — no data loss.
   - `docker start kind-worker<N>` to restore the node before the next test.
4. Repeat the node-loss test specifically for the Ray head pod's node: confirm GCS state recovers from Redis and the Serve endpoint resumes serving without a full `helm upgrade`/redeploy.
5. Record pass/fail for each component (redpanda, clickhouse, serve/Ray, pipeline, mitigation) against "survives node loss" — this is the exit-criteria evidence for Phase 4a and the baseline that Phase 4b's security work and Phase 4c's DR work build on top of.

## Explicitly Out of Scope (deferred to later sub-phases)

- mTLS between services, RBAC, NetworkPolicies, secrets-manager integration (Vault/sealed-secrets) — Phase 4b.
- Backup/restore runbooks, platform SLOs, formal DR drills — Phase 4c.
- Real (non-self-signed) TLS certificates via cert-manager/ACME — Phase 4b, once nginx-ingress is in place to attach `cert-manager` annotations to.
- Security review sign-off — depends on Phase 4b being complete; not a Phase 4a exit criterion.
