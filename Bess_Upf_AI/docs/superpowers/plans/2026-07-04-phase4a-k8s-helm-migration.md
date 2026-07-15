# Phase 4a: k8s/Helm Migration Baseline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the 5 CoreWatch v2 services (redpanda, clickhouse, serve, pipeline, mitigation) on a local 4-node `kind` cluster via one umbrella Helm chart, with HA for the 3 stateful/critical components, and prove survival of a node loss with a repeatable test.

**Architecture:** `kind` cluster (1 control-plane + 3 workers) → `bess-upf` namespace → umbrella chart `charts/bess-upf/` nesting 5 hand-rolled subcharts (redpanda, clickhouse, serve, pipeline, mitigation) plus `ingress-nginx` as a real chart dependency. redpanda and clickhouse run as 3-replica StatefulSets (RF=3 / ReplicatedMergeTree+Keeper quorum). serve runs via the KubeRay operator's `RayCluster` CR with external-Redis GCS fault tolerance. pipeline and mitigation are plain 2-replica Deployments. Images build from the existing Dockerfiles, unchanged, and push to GHCR.

**Tech Stack:** kind v0.32, Helm 4.2, kubectl 1.34, KubeRay operator, ClickHouse 24.3 embedded Keeper, Redpanda v23.2.19, Ray 2.30.0, GHCR.

**Deviations from the design spec, decided during planning (not re-litigated — see spec `docs/superpowers/specs/2026-07-04-phase4a-k8s-helm-migration-design.md`):**
1. **Ingress backends.** The spec's line about translating `/vm/*`, `/api/*`→analysis, `/grafana/*`, `/*`→frontend from the Caddyfile refers to `config/caddy/Caddyfile`, which is wired into the **v1** stack (`docker-compose.yml`/`core`/`base`), not `docker-compose.v2.yml`. None of victoriametrics/analysis/grafana/frontend/minio are in this migration's 6-service scope. Ingress in this plan routes only to the two HTTP services that actually exist here: `/api/serve/*` → `serve:8000`, `/api/mitigation/*` → `mitigation:8081`.
2. **Subchart dependency mechanism.** The spec says the umbrella `Chart.yaml` declares "dependencies on 6 subcharts." Helm's `file://` dependency indirection for local subcharts is fragile and adds no value over the standard approach — the 5 local subcharts are physically nested under `charts/bess-upf/charts/<name>/` (Helm auto-discovers them, no `dependencies:` entry needed). Only `ingress-nginx`, a real external chart, gets a `dependencies:` entry in `Chart.yaml`. Functionally identical outcome (one `helm install` brings up all 6), simpler mechanism.
3. **ClickHouse Keeper placement.** Spec allows Keeper "as sidecars" on the 3 CH pods. Simpler: ClickHouse's server binary has embedded Keeper support (`<keeper_server>` config block) — one process per pod serves both roles, no second container needed.

---

## Task 0: kind cluster bootstrap + cluster-level prerequisites

**Files:**
- Create: `k8s/kind-config.yaml`
- Create: `scripts/k8s-bootstrap.sh`

- [ ] **Step 1: Write the kind cluster config**

`k8s/kind-config.yaml`:
```yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
  - role: worker
    extraMounts:
      - hostPath: ./models
        containerPath: /models
  - role: worker
    extraMounts:
      - hostPath: ./models
        containerPath: /models
  - role: worker
    extraMounts:
      - hostPath: ./models
        containerPath: /models
```

`extraMounts` on every worker (not just one) because a pod using the `/models` hostPath can be scheduled onto any of the 3 workers, and this is how the `serve` RayCluster pods (Task 5) get model files.

- [ ] **Step 2: Write the bootstrap script**

`scripts/k8s-bootstrap.sh`:
```bash
#!/bin/bash
set -euo pipefail

kind create cluster --name bess-upf --config k8s/kind-config.yaml
kubectl create namespace bess-upf --dry-run=client -o yaml | kubectl apply -f -

helm repo add kuberay https://ray-project.github.io/kuberay-helm/
helm repo update
helm upgrade --install kuberay-operator kuberay/kuberay-operator \
  --namespace bess-upf --version 1.1.0 --wait

kubectl create secret docker-registry ghcr-pull-secret \
  --namespace bess-upf \
  --docker-server=ghcr.io \
  --docker-username="${GHCR_USERNAME:?set GHCR_USERNAME}" \
  --docker-password="${GHCR_TOKEN:?set GHCR_TOKEN}" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "Cluster ready. Nodes:"
kubectl get nodes
```

- [ ] **Step 3: Run it and verify**

Prereq: Docker Desktop must be running (`docker ps` must succeed — it currently isn't on this machine, start it first).

```bash
chmod +x scripts/k8s-bootstrap.sh
GHCR_USERNAME=that-geekyguy GHCR_TOKEN=<your-ghcr-pat-packages:read+write> ./scripts/k8s-bootstrap.sh
```
Expected: `kubectl get nodes` lists 4 nodes (1 control-plane, 3 workers) all `Ready`; `kubectl get pods -n bess-upf` shows a `kuberay-operator-*` pod `Running`; `kubectl get secret -n bess-upf ghcr-pull-secret` exists.

- [ ] **Step 4: Commit**

```bash
git add k8s/kind-config.yaml scripts/k8s-bootstrap.sh
git commit -m "feat(k8s): kind cluster config + bootstrap script (kuberay operator, ghcr secret)"
```

---

## Task 1: Build and push service images to GHCR

**Files:**
- Create: `scripts/build-push-images.sh`

- [ ] **Step 1: Write the build/push script**

`scripts/build-push-images.sh`:
```bash
#!/bin/bash
set -euo pipefail

REGISTRY="${REGISTRY:-ghcr.io/that-geekyguy}"
TAG="${TAG:-phase4a}"

for svc in serve pipeline mitigation; do
  echo "Building ${svc}..."
  docker build -t "${REGISTRY}/bess-upf-${svc}:${TAG}" "./${svc}"
  docker push "${REGISTRY}/bess-upf-${svc}:${TAG}"
done

echo "Pushed images:"
for svc in serve pipeline mitigation; do
  echo "  ${REGISTRY}/bess-upf-${svc}:${TAG}"
done
```

redpanda and clickhouse use unmodified upstream images (`docker.redpanda.com/redpandadata/redpanda:v23.2.19`, `clickhouse/clickhouse-server:24.3-alpine`) — no build/push needed for those.

- [ ] **Step 2: Run it and verify**

```bash
chmod +x scripts/build-push-images.sh
docker login ghcr.io -u that-geekyguy   # paste PAT with write:packages when prompted
REGISTRY=ghcr.io/that-geekyguy TAG=phase4a ./scripts/build-push-images.sh
```
Expected: 3 successful `docker push` runs, each ending with a digest line (`phase4a: digest: sha256:... size: ...`).

- [ ] **Step 3: Commit**

```bash
git add scripts/build-push-images.sh
git commit -m "feat(k8s): script to build+push serve/pipeline/mitigation images to GHCR"
```

---

## Task 2: Umbrella chart skeleton

**Files:**
- Create: `charts/bess-upf/Chart.yaml`
- Create: `charts/bess-upf/values.yaml`
- Modify: `.gitignore`

- [ ] **Step 1: Write Chart.yaml**

`charts/bess-upf/Chart.yaml`:
```yaml
apiVersion: v2
name: bess-upf
description: BESS-UPF v2 stack — Phase 4a k8s/Helm baseline
version: 0.1.0
appVersion: "phase4a"
dependencies:
  - name: ingress-nginx
    version: "4.11.3"
    repository: "https://kubernetes.github.io/ingress-nginx"
    condition: ingress-nginx.enabled
```

- [ ] **Step 2: Write values.yaml**

`charts/bess-upf/values.yaml`:
```yaml
global:
  registry: ghcr.io/that-geekyguy
  tag: phase4a
  imagePullSecret: ghcr-pull-secret
  namespace: bess-upf

ingress-nginx:
  enabled: true
  controller:
    service:
      type: NodePort
```

- [ ] **Step 3: Ignore fetched chart archives**

Add to `.gitignore`:
```
charts/bess-upf/charts/*.tgz
```
(The 5 local subcharts, added in later tasks, are plain directories and stay committed — this line only ignores the `ingress-nginx` `.tgz` that `helm dependency update` downloads.)

- [ ] **Step 4: Fetch the ingress-nginx dependency and verify**

```bash
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm repo update
helm dependency update charts/bess-upf
```
Expected: `charts/bess-upf/charts/ingress-nginx-4.11.3.tgz` created; `charts/bess-upf/Chart.lock` created.

- [ ] **Step 5: Commit**

```bash
git add charts/bess-upf/Chart.yaml charts/bess-upf/values.yaml charts/bess-upf/Chart.lock .gitignore
git commit -m "feat(k8s): umbrella chart skeleton + ingress-nginx dependency"
```

---

## Task 3: redpanda subchart (StatefulSet, RF=3, PDB)

**Files:**
- Create: `charts/bess-upf/charts/redpanda/Chart.yaml`
- Create: `charts/bess-upf/charts/redpanda/values.yaml`
- Create: `charts/bess-upf/charts/redpanda/templates/statefulset.yaml`
- Create: `charts/bess-upf/charts/redpanda/templates/service.yaml`
- Create: `charts/bess-upf/charts/redpanda/templates/pdb.yaml`
- Create: `charts/bess-upf/charts/redpanda/templates/topic-init-job.yaml`

- [ ] **Step 1: Chart.yaml + values.yaml**

`charts/bess-upf/charts/redpanda/Chart.yaml`:
```yaml
apiVersion: v2
name: redpanda
description: 3-node Redpanda StatefulSet, RF=3
version: 0.1.0
```

`charts/bess-upf/charts/redpanda/values.yaml`:
```yaml
namespace: bess-upf
replicas: 3
image: docker.redpanda.com/redpandadata/redpanda:v23.2.19
storage: 2Gi
```

- [ ] **Step 2: Services (headless for peer discovery, client for consumers)**

`charts/bess-upf/charts/redpanda/templates/service.yaml`:
```yaml
apiVersion: v1
kind: Service
metadata:
  name: redpanda-headless
  namespace: {{ .Values.namespace }}
spec:
  clusterIP: None
  selector:
    app: redpanda
  ports:
    - name: kafka
      port: 9092
    - name: rpc
      port: 33145
    - name: admin
      port: 9644
---
apiVersion: v1
kind: Service
metadata:
  name: redpanda
  namespace: {{ .Values.namespace }}
spec:
  selector:
    app: redpanda
  ports:
    - name: kafka
      port: 9092
      targetPort: 9092
```

- [ ] **Step 3: StatefulSet**

`charts/bess-upf/charts/redpanda/templates/statefulset.yaml`:
```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: redpanda
  namespace: {{ .Values.namespace }}
spec:
  serviceName: redpanda-headless
  replicas: {{ .Values.replicas }}
  podManagementPolicy: Parallel
  selector:
    matchLabels:
      app: redpanda
  template:
    metadata:
      labels:
        app: redpanda
    spec:
      containers:
        - name: redpanda
          image: {{ .Values.image }}
          command: ["bash", "-c"]
          args:
            - |
              set -e
              ORDINAL=${HOSTNAME##*-}
              exec redpanda start \
                --smp 1 --memory 1G --reserve-memory 0M --overprovisioned \
                --node-id ${ORDINAL} \
                --seeds "redpanda-0.redpanda-headless.{{ .Values.namespace }}.svc.cluster.local:33145" \
                --kafka-addr internal://0.0.0.0:9092 \
                --advertise-kafka-addr internal://$(hostname -f):9092 \
                --rpc-addr 0.0.0.0:33145 \
                --advertise-rpc-addr $(hostname -f):33145
          ports:
            - containerPort: 9092
              name: kafka
            - containerPort: 33145
              name: rpc
            - containerPort: 9644
              name: admin
          volumeMounts:
            - name: data
              mountPath: /var/lib/redpanda/data
          readinessProbe:
            exec:
              command: ["rpk", "cluster", "health"]
            initialDelaySeconds: 10
            periodSeconds: 10
            failureThreshold: 6
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: ["ReadWriteOnce"]
        resources:
          requests:
            storage: {{ .Values.storage }}
```

- [ ] **Step 4: PodDisruptionBudget**

`charts/bess-upf/charts/redpanda/templates/pdb.yaml`:
```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: redpanda-pdb
  namespace: {{ .Values.namespace }}
spec:
  maxUnavailable: 1
  selector:
    matchLabels:
      app: redpanda
```

- [ ] **Step 5: Topic-creation Job (RF=3), runs once after install**

`charts/bess-upf/charts/redpanda/templates/topic-init-job.yaml`:
```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: redpanda-topic-init
  namespace: {{ .Values.namespace }}
  annotations:
    "helm.sh/hook": post-install,post-upgrade
    "helm.sh/hook-weight": "5"
    "helm.sh/hook-delete-policy": before-hook-creation
spec:
  template:
    spec:
      restartPolicy: OnFailure
      containers:
        - name: create-topics
          image: {{ .Values.image }}
          command: ["bash", "-c"]
          args:
            - |
              set -e
              until rpk cluster health --brokers redpanda:9092 2>/dev/null | grep -q "Healthy:.*true"; do
                echo "waiting for redpanda cluster..."; sleep 3
              done
              rpk topic create upf.metrics.raw upf.anomalies.critical \
                upf.shadow.detections upf.action_audit \
                --brokers redpanda:9092 --replicas 3 || true
```

- [ ] **Step 6: Lint and verify templates render**

```bash
helm lint charts/bess-upf/charts/redpanda
helm template charts/bess-upf/charts/redpanda | kubectl apply --dry-run=client -f -
```
Expected: `1 chart(s) linted, 0 chart(s) failed`; dry-run apply reports all resources created (no schema errors).

- [ ] **Step 7: Commit**

```bash
git add charts/bess-upf/charts/redpanda
git commit -m "feat(k8s): redpanda subchart — 3-replica StatefulSet, PDB, RF=3 topic init"
```

---

## Task 4: clickhouse subchart (StatefulSet + embedded Keeper quorum + ReplicatedMergeTree)

**Files:**
- Create: `charts/bess-upf/charts/clickhouse/Chart.yaml`
- Create: `charts/bess-upf/charts/clickhouse/values.yaml`
- Create: `charts/bess-upf/charts/clickhouse/files/init.sql`
- Create: `charts/bess-upf/charts/clickhouse/templates/configmap.yaml`
- Create: `charts/bess-upf/charts/clickhouse/templates/secret.yaml`
- Create: `charts/bess-upf/charts/clickhouse/templates/statefulset.yaml`
- Create: `charts/bess-upf/charts/clickhouse/templates/service.yaml`
- Create: `charts/bess-upf/charts/clickhouse/templates/pdb.yaml`

- [ ] **Step 1: Chart.yaml + values.yaml**

`charts/bess-upf/charts/clickhouse/Chart.yaml`:
```yaml
apiVersion: v2
name: clickhouse
description: 3-node ClickHouse StatefulSet with embedded Keeper quorum
version: 0.1.0
```

`charts/bess-upf/charts/clickhouse/values.yaml`:
```yaml
namespace: bess-upf
image: clickhouse/clickhouse-server:24.3-alpine
user: chuser
password: localdev123
storage: 5Gi
```

- [ ] **Step 2: k8s-specific init.sql — ReplicatedMergeTree variants**

`charts/bess-upf/charts/clickhouse/files/init.sql` (copy of `config/clickhouse/init.sql` with the 4 local tables' engines swapped for their `Replicated*` equivalents — Kafka-engine tables and materialized views are unchanged since replication is handled at the MergeTree layer, not the Kafka consumer layer):

```sql
-- charts/bess-upf/charts/clickhouse/files/init.sql
-- k8s variant of config/clickhouse/init.sql: MergeTree -> ReplicatedMergeTree
-- so each of the 3 StatefulSet replicas holds a full copy, synced via Keeper.
CREATE DATABASE IF NOT EXISTS bess_upf;

USE bess_upf;

------------------------------------------------------------------------
-- upf_metrics
------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS upf_metrics_kafka (
    upf_id                     String,
    ts                         Float64,
    pfcp_sessions_total        UInt64,
    port_bytes_N3_rx           UInt64,   port_bytes_N3_rx_rate   Float64,
    port_bytes_N6_tx           UInt64,   port_bytes_N6_tx_rate   Float64,
    port_pkts_N3_rx            UInt64,   port_pkts_N3_rx_rate    Float64,
    port_pkts_N6_tx            UInt64,   port_pkts_N6_tx_rate    Float64,
    port_dropped_N3_rx         UInt64,   port_dropped_N3_rx_rate Float64,
    port_dropped_N6_rx         UInt64,   port_dropped_N6_rx_rate Float64,
    upf_sim_scenario_normal    UInt8,    upf_sim_scenario_congestion UInt8,
    upf_sim_scenario_flatline  UInt8,    upf_sim_scenario_spike  UInt8,
    go_goroutines              UInt64,   go_heap_alloc_bytes     UInt64,
    process_cpu_seconds_total  Float64,  process_cpu_rate        Float64
) ENGINE = Kafka
SETTINGS
    kafka_broker_list          = 'redpanda:9092',
    kafka_topic_list           = 'upf.metrics.raw',
    kafka_group_name           = 'clickhouse-metrics',
    kafka_format               = 'JSONEachRow',
    kafka_skip_broken_messages = 1;

CREATE TABLE IF NOT EXISTS upf_metrics (
    upf_id                     LowCardinality(String),
    ts                         DateTime64(3),
    pfcp_sessions_total        UInt64,
    port_bytes_N3_rx           UInt64,   port_bytes_N3_rx_rate   Float64,
    port_bytes_N6_tx           UInt64,   port_bytes_N6_tx_rate   Float64,
    port_pkts_N3_rx            UInt64,   port_pkts_N3_rx_rate    Float64,
    port_pkts_N6_tx            UInt64,   port_pkts_N6_tx_rate    Float64,
    port_dropped_N3_rx         UInt64,   port_dropped_N3_rx_rate Float64,
    port_dropped_N6_rx         UInt64,   port_dropped_N6_rx_rate Float64,
    upf_sim_scenario_normal    UInt8,    upf_sim_scenario_congestion UInt8,
    upf_sim_scenario_flatline  UInt8,    upf_sim_scenario_spike  UInt8,
    go_goroutines              UInt64,   go_heap_alloc_bytes     UInt64,
    process_cpu_seconds_total  Float64,  process_cpu_rate        Float64
) ENGINE = ReplicatedMergeTree('/clickhouse/tables/{shard}/upf_metrics', '{replica}')
PARTITION BY toYYYYMMDD(ts)
ORDER BY (upf_id, ts)
TTL toDateTime(ts) + INTERVAL 90 DAY;

CREATE MATERIALIZED VIEW IF NOT EXISTS upf_metrics_mv TO upf_metrics AS
SELECT
    upf_id,
    toDateTime64(ts, 3) AS ts,
    pfcp_sessions_total,
    port_bytes_N3_rx,    port_bytes_N3_rx_rate,
    port_bytes_N6_tx,    port_bytes_N6_tx_rate,
    port_pkts_N3_rx,     port_pkts_N3_rx_rate,
    port_pkts_N6_tx,     port_pkts_N6_tx_rate,
    port_dropped_N3_rx,  port_dropped_N3_rx_rate,
    port_dropped_N6_rx,  port_dropped_N6_rx_rate,
    upf_sim_scenario_normal, upf_sim_scenario_congestion,
    upf_sim_scenario_flatline, upf_sim_scenario_spike,
    go_goroutines,       go_heap_alloc_bytes,
    process_cpu_seconds_total, process_cpu_rate
FROM upf_metrics_kafka;

------------------------------------------------------------------------
-- anomaly_events
------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS anomaly_events_kafka (
    upf_id                  String,
    ts                      Float64,
    anomaly                 UInt8,
    anomaly_score           Float64,
    threshold               Float64,
    top_anomalous_channels  Array(String),
    model_version           String,
    window_end_offset       Int64
) ENGINE = Kafka
SETTINGS
    kafka_broker_list          = 'redpanda:9092',
    kafka_topic_list           = 'upf.anomalies.critical',
    kafka_group_name           = 'clickhouse-anomaly',
    kafka_format               = 'JSONEachRow',
    kafka_skip_broken_messages = 1;

CREATE TABLE IF NOT EXISTS anomaly_events (
    upf_id                  LowCardinality(String),
    ts                      DateTime64(3),
    anomaly                 UInt8,
    anomaly_score           Float64,
    threshold               Float64,
    top_anomalous_channels  Array(String),
    model_version           String,
    window_end_offset       Int64
) ENGINE = ReplicatedReplacingMergeTree('/clickhouse/tables/{shard}/anomaly_events', '{replica}', ts)
PARTITION BY toYYYYMMDD(ts)
ORDER BY (upf_id, ts)
TTL toDateTime(ts) + INTERVAL 90 DAY;

CREATE MATERIALIZED VIEW IF NOT EXISTS anomaly_events_mv TO anomaly_events AS
SELECT
    upf_id,
    toDateTime64(ts, 3) AS ts,
    anomaly, anomaly_score, threshold,
    top_anomalous_channels, model_version, window_end_offset
FROM anomaly_events_kafka;

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
) ENGINE = ReplicatedMergeTree('/clickhouse/tables/{shard}/shadow_detections', '{replica}')
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

-- Phase 3: action audit
CREATE TABLE IF NOT EXISTS action_audit_kafka (
    action_id      String,
    upf_id         String,
    ts             Float64,
    action_class   String,
    trust_level    String,
    dry_run        UInt8,
    success        UInt8,
    message        String,
    anomaly_score  Float64,
    model_version  String,
    rollback_token String
) ENGINE = Kafka SETTINGS
    kafka_broker_list = 'redpanda:9092',
    kafka_topic_list  = 'upf.action_audit',
    kafka_group_name  = 'clickhouse-audit',
    kafka_format      = 'JSONEachRow',
    kafka_skip_broken_messages = 1;

CREATE TABLE IF NOT EXISTS action_audit (
    action_id      String,
    upf_id         LowCardinality(String),
    ts             DateTime64(3),
    action_class   LowCardinality(String),
    trust_level    LowCardinality(String),
    dry_run        UInt8,
    success        UInt8,
    message        String,
    anomaly_score  Float64,
    model_version  LowCardinality(String),
    rollback_token String
) ENGINE = ReplicatedMergeTree('/clickhouse/tables/{shard}/action_audit', '{replica}')
PARTITION BY toYYYYMMDD(ts)
ORDER BY (upf_id, ts)
TTL toDateTime(ts) + INTERVAL 365 DAY;

CREATE MATERIALIZED VIEW IF NOT EXISTS action_audit_mv TO action_audit AS
SELECT
    action_id,
    upf_id,
    toDateTime64(ts, 3) AS ts,
    action_class,
    trust_level,
    dry_run,
    success,
    message,
    anomaly_score,
    model_version,
    rollback_token
FROM action_audit_kafka;
```

- [ ] **Step 3: ConfigMap — cluster/keeper/macros XML + init.sql**

`charts/bess-upf/charts/clickhouse/templates/configmap.yaml`:
```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: clickhouse-config
  namespace: {{ .Values.namespace }}
data:
  cluster.xml: |
    <clickhouse>
        <remote_servers>
            <bess_upf_cluster>
                <shard>
                    <replica>
                        <host>clickhouse-0.clickhouse-headless.{{ .Values.namespace }}.svc.cluster.local</host>
                        <port>9000</port>
                    </replica>
                    <replica>
                        <host>clickhouse-1.clickhouse-headless.{{ .Values.namespace }}.svc.cluster.local</host>
                        <port>9000</port>
                    </replica>
                    <replica>
                        <host>clickhouse-2.clickhouse-headless.{{ .Values.namespace }}.svc.cluster.local</host>
                        <port>9000</port>
                    </replica>
                </shard>
            </bess_upf_cluster>
        </remote_servers>

        <zookeeper>
            <node><host>clickhouse-0.clickhouse-headless.{{ .Values.namespace }}.svc.cluster.local</host><port>9181</port></node>
            <node><host>clickhouse-1.clickhouse-headless.{{ .Values.namespace }}.svc.cluster.local</host><port>9181</port></node>
            <node><host>clickhouse-2.clickhouse-headless.{{ .Values.namespace }}.svc.cluster.local</host><port>9181</port></node>
        </zookeeper>

        <keeper_server>
            <tcp_port>9181</tcp_port>
            <server_id from_env="KEEPER_SERVER_ID"/>
            <log_storage_path>/var/lib/clickhouse/coordination/log</log_storage_path>
            <snapshot_storage_path>/var/lib/clickhouse/coordination/snapshots</snapshot_storage_path>
            <coordination_settings>
                <operation_timeout_ms>10000</operation_timeout_ms>
                <session_timeout_ms>30000</session_timeout_ms>
                <raft_logs_level>warning</raft_logs_level>
            </coordination_settings>
            <raft_configuration>
                <server>
                    <id>1</id>
                    <hostname>clickhouse-0.clickhouse-headless.{{ .Values.namespace }}.svc.cluster.local</hostname>
                    <port>9234</port>
                </server>
                <server>
                    <id>2</id>
                    <hostname>clickhouse-1.clickhouse-headless.{{ .Values.namespace }}.svc.cluster.local</hostname>
                    <port>9234</port>
                </server>
                <server>
                    <id>3</id>
                    <hostname>clickhouse-2.clickhouse-headless.{{ .Values.namespace }}.svc.cluster.local</hostname>
                    <port>9234</port>
                </server>
            </raft_configuration>
        </keeper_server>
    </clickhouse>
  macros.xml: |
    <clickhouse>
        <macros>
            <shard>1</shard>
            <replica from_env="POD_NAME"/>
        </macros>
    </clickhouse>
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: clickhouse-initdb
  namespace: {{ .Values.namespace }}
data:
{{ (.Files.Glob "files/init.sql").AsConfig | indent 2 }}
```

- [ ] **Step 4: Secret for CLICKHOUSE_PASSWORD**

`charts/bess-upf/charts/clickhouse/templates/secret.yaml`:
```yaml
apiVersion: v1
kind: Secret
metadata:
  name: clickhouse-credentials
  namespace: {{ .Values.namespace }}
type: Opaque
stringData:
  password: {{ .Values.password | quote }}
```

- [ ] **Step 5: StatefulSet**

`charts/bess-upf/charts/clickhouse/templates/statefulset.yaml`:
```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: clickhouse
  namespace: {{ .Values.namespace }}
spec:
  serviceName: clickhouse-headless
  replicas: 3
  podManagementPolicy: Parallel
  selector:
    matchLabels:
      app: clickhouse
  template:
    metadata:
      labels:
        app: clickhouse
    spec:
      containers:
        - name: clickhouse
          image: {{ .Values.image }}
          command: ["bash", "-c"]
          args:
            - |
              set -e
              ORDINAL=${HOSTNAME##*-}
              export KEEPER_SERVER_ID=$((ORDINAL + 1))
              export POD_NAME=${HOSTNAME}
              exec /entrypoint.sh
          env:
            - name: CLICKHOUSE_USER
              value: {{ .Values.user }}
            - name: CLICKHOUSE_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: clickhouse-credentials
                  key: password
            - name: CLICKHOUSE_DB
              value: bess_upf
          ports:
            - containerPort: 8123
              name: http
            - containerPort: 9000
              name: native
            - containerPort: 9181
              name: keeper
            - containerPort: 9234
              name: keeper-raft
          volumeMounts:
            - name: data
              mountPath: /var/lib/clickhouse
            - name: config
              mountPath: /etc/clickhouse-server/config.d/cluster.xml
              subPath: cluster.xml
            - name: config
              mountPath: /etc/clickhouse-server/config.d/macros.xml
              subPath: macros.xml
            - name: initdb
              mountPath: /docker-entrypoint-initdb.d/init.sql
              subPath: init.sql
          readinessProbe:
            exec:
              command: ["clickhouse-client", "--query", "SELECT 1"]
            initialDelaySeconds: 20
            periodSeconds: 10
            failureThreshold: 6
      volumes:
        - name: config
          configMap:
            name: clickhouse-config
        - name: initdb
          configMap:
            name: clickhouse-initdb
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: ["ReadWriteOnce"]
        resources:
          requests:
            storage: {{ .Values.storage }}
```

- [ ] **Step 6: Services + PDB**

`charts/bess-upf/charts/clickhouse/templates/service.yaml`:
```yaml
apiVersion: v1
kind: Service
metadata:
  name: clickhouse-headless
  namespace: {{ .Values.namespace }}
spec:
  clusterIP: None
  selector:
    app: clickhouse
  ports:
    - name: http
      port: 8123
    - name: native
      port: 9000
    - name: keeper
      port: 9181
    - name: keeper-raft
      port: 9234
---
apiVersion: v1
kind: Service
metadata:
  name: clickhouse
  namespace: {{ .Values.namespace }}
spec:
  selector:
    app: clickhouse
  ports:
    - name: http
      port: 8123
      targetPort: 8123
    - name: native
      port: 9000
      targetPort: 9000
```

`charts/bess-upf/charts/clickhouse/templates/pdb.yaml`:
```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: clickhouse-pdb
  namespace: {{ .Values.namespace }}
spec:
  maxUnavailable: 1
  selector:
    matchLabels:
      app: clickhouse
```

- [ ] **Step 7: Lint and dry-run verify**

```bash
helm lint charts/bess-upf/charts/clickhouse
helm template charts/bess-upf/charts/clickhouse | kubectl apply --dry-run=client -f -
```
Expected: lint passes; dry-run apply succeeds for all 7 resources (2 ConfigMaps, 1 Secret, 1 StatefulSet, 2 Services, 1 PDB).

- [ ] **Step 8: Commit**

```bash
git add charts/bess-upf/charts/clickhouse
git commit -m "feat(k8s): clickhouse subchart — 3-replica StatefulSet, embedded Keeper quorum, ReplicatedMergeTree schema"
```

---

## Task 5: serve subchart (KubeRay RayCluster + Redis GCS backing store)

**Files:**
- Create: `charts/bess-upf/charts/serve/Chart.yaml`
- Create: `charts/bess-upf/charts/serve/values.yaml`
- Create: `charts/bess-upf/charts/serve/templates/redis.yaml`
- Create: `charts/bess-upf/charts/serve/templates/raycluster.yaml`
- Create: `charts/bess-upf/charts/serve/templates/service.yaml`

- [ ] **Step 1: Chart.yaml + values.yaml**

`charts/bess-upf/charts/serve/Chart.yaml`:
```yaml
apiVersion: v2
name: serve
description: Ray Serve inference via KubeRay RayCluster, GCS fault tolerance via external Redis
version: 0.1.0
```

`charts/bess-upf/charts/serve/values.yaml`:
```yaml
namespace: bess-upf
registry: ghcr.io/that-geekyguy
tag: phase4a
imagePullSecret: ghcr-pull-secret
workerReplicas: 2
```

- [ ] **Step 2: Redis for GCS fault tolerance**

`charts/bess-upf/charts/serve/templates/redis.yaml`:
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ray-gcs-redis
  namespace: {{ .Values.namespace }}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: ray-gcs-redis
  template:
    metadata:
      labels:
        app: ray-gcs-redis
    spec:
      containers:
        - name: redis
          image: redis:7.2-alpine
          ports:
            - containerPort: 6379
---
apiVersion: v1
kind: Service
metadata:
  name: ray-gcs-redis
  namespace: {{ .Values.namespace }}
spec:
  selector:
    app: ray-gcs-redis
  ports:
    - port: 6379
```

- [ ] **Step 3: RayCluster custom resource**

`charts/bess-upf/charts/serve/templates/raycluster.yaml`:
```yaml
apiVersion: ray.io/v1
kind: RayCluster
metadata:
  name: bess-upf-serve
  namespace: {{ .Values.namespace }}
spec:
  rayVersion: "2.30.0"
  gcsFaultToleranceOptions:
    redisAddress: "ray-gcs-redis.{{ .Values.namespace }}.svc.cluster.local:6379"
  headGroupSpec:
    serviceType: ClusterIP
    rayStartParams:
      dashboard-host: "0.0.0.0"
    template:
      spec:
        imagePullSecrets:
          - name: {{ .Values.imagePullSecret }}
        containers:
          - name: ray-head
            image: "{{ .Values.registry }}/bess-upf-serve:{{ .Values.tag }}"
            env:
              - name: MODELS_DIR
                value: /models
              - name: KAFKA_BROKER
                value: redpanda:9092
              - name: SHADOW_TOPIC
                value: upf.shadow.detections
            ports:
              - containerPort: 8000
                name: serve
              - containerPort: 6379
                name: gcs
              - containerPort: 8265
                name: dashboard
            volumeMounts:
              - name: models
                mountPath: /models
                readOnly: true
        volumes:
          - name: models
            hostPath:
              path: /models
  workerGroupSpecs:
    - groupName: default
      replicas: {{ .Values.workerReplicas }}
      minReplicas: {{ .Values.workerReplicas }}
      maxReplicas: {{ .Values.workerReplicas }}
      rayStartParams: {}
      template:
        spec:
          imagePullSecrets:
            - name: {{ .Values.imagePullSecret }}
          containers:
            - name: ray-worker
              image: "{{ .Values.registry }}/bess-upf-serve:{{ .Values.tag }}"
              env:
                - name: MODELS_DIR
                  value: /models
              volumeMounts:
                - name: models
                  mountPath: /models
                  readOnly: true
          volumes:
            - name: models
              hostPath:
                path: /models
```

- [ ] **Step 4: Stable Service name for the head pod's Serve port**

`charts/bess-upf/charts/serve/templates/service.yaml`:
```yaml
apiVersion: v1
kind: Service
metadata:
  name: serve
  namespace: {{ .Values.namespace }}
spec:
  selector:
    ray.io/cluster: bess-upf-serve
    ray.io/node-type: head
  ports:
    - name: http
      port: 8000
      targetPort: 8000
```

- [ ] **Step 5: Lint and verify (CRD-dependent, dry-run needs the cluster from Task 0)**

```bash
helm lint charts/bess-upf/charts/serve
helm template charts/bess-upf/charts/serve | kubectl apply --dry-run=client -f -
```
Expected: lint passes. Dry-run against the live cluster (kuberay-operator installed in Task 0 registers the `RayCluster` CRD) succeeds; against a cluster without the operator it will fail with `no matches for kind "RayCluster"` — that's expected before Task 0 runs, not a bug in this chart.

- [ ] **Step 6: Commit**

```bash
git add charts/bess-upf/charts/serve
git commit -m "feat(k8s): serve subchart — RayCluster CR via KubeRay, Redis-backed GCS fault tolerance"
```

---

## Task 6: pipeline subchart (Deployment, 2 replicas)

**Files:**
- Create: `charts/bess-upf/charts/pipeline/Chart.yaml`
- Create: `charts/bess-upf/charts/pipeline/values.yaml`
- Create: `charts/bess-upf/charts/pipeline/templates/deployment.yaml`

- [ ] **Step 1: Chart.yaml + values.yaml**

`charts/bess-upf/charts/pipeline/Chart.yaml`:
```yaml
apiVersion: v2
name: pipeline
description: Bytewax stream processor, 2 replicas
version: 0.1.0
```

`charts/bess-upf/charts/pipeline/values.yaml`:
```yaml
namespace: bess-upf
registry: ghcr.io/that-geekyguy
tag: phase4a
imagePullSecret: ghcr-pull-secret
replicas: 2
```

- [ ] **Step 2: Deployment**

`charts/bess-upf/charts/pipeline/templates/deployment.yaml`:
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: pipeline
  namespace: {{ .Values.namespace }}
spec:
  replicas: {{ .Values.replicas }}
  selector:
    matchLabels:
      app: pipeline
  template:
    metadata:
      labels:
        app: pipeline
    spec:
      imagePullSecrets:
        - name: {{ .Values.imagePullSecret }}
      containers:
        - name: pipeline
          image: "{{ .Values.registry }}/bess-upf-pipeline:{{ .Values.tag }}"
          env:
            - name: KAFKA_BROKER
              value: redpanda:9092
            - name: RAY_SERVE_URL
              value: http://serve:8000
            - name: BYTEWAX_CHECKPOINT_DIR
              value: /data/bytewax-checkpoint
            - name: SCRAPE_INTERVAL_SECS
              value: "1.0"
          volumeMounts:
            - name: data
              mountPath: /data
      volumes:
        - name: data
          emptyDir: {}
```
`emptyDir` (not a PVC) because per the spec, Bytewax checkpoint state doesn't need cross-restart durability at this layer — exactly-once semantics come from Kafka/ClickHouse, not this local directory.

- [ ] **Step 3: Lint and verify**

```bash
helm lint charts/bess-upf/charts/pipeline
helm template charts/bess-upf/charts/pipeline | kubectl apply --dry-run=client -f -
```
Expected: lint passes, dry-run succeeds.

- [ ] **Step 4: Commit**

```bash
git add charts/bess-upf/charts/pipeline
git commit -m "feat(k8s): pipeline subchart — 2-replica Deployment"
```

---

## Task 7: mitigation subchart (Deployment, 2 replicas, Service :8081)

**Files:**
- Create: `charts/bess-upf/charts/mitigation/Chart.yaml`
- Create: `charts/bess-upf/charts/mitigation/values.yaml`
- Create: `charts/bess-upf/charts/mitigation/templates/deployment.yaml`
- Create: `charts/bess-upf/charts/mitigation/templates/service.yaml`

- [ ] **Step 1: Chart.yaml + values.yaml**

`charts/bess-upf/charts/mitigation/Chart.yaml`:
```yaml
apiVersion: v2
name: mitigation
description: Closed-loop mitigation engine, 2 replicas
version: 0.1.0
```

`charts/bess-upf/charts/mitigation/values.yaml`:
```yaml
namespace: bess-upf
registry: ghcr.io/that-geekyguy
tag: phase4a
imagePullSecret: ghcr-pull-secret
replicas: 2
```

- [ ] **Step 2: Deployment**

`charts/bess-upf/charts/mitigation/templates/deployment.yaml`:
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: mitigation
  namespace: {{ .Values.namespace }}
spec:
  replicas: {{ .Values.replicas }}
  selector:
    matchLabels:
      app: mitigation
  template:
    metadata:
      labels:
        app: mitigation
    spec:
      imagePullSecrets:
        - name: {{ .Values.imagePullSecret }}
      containers:
        - name: mitigation
          image: "{{ .Values.registry }}/bess-upf-mitigation:{{ .Values.tag }}"
          env:
            - name: KAFKA_BROKER
              value: redpanda:9092
            - name: API_PORT
              value: "8081"
          ports:
            - containerPort: 8081
```

- [ ] **Step 3: Service**

`charts/bess-upf/charts/mitigation/templates/service.yaml`:
```yaml
apiVersion: v1
kind: Service
metadata:
  name: mitigation
  namespace: {{ .Values.namespace }}
spec:
  selector:
    app: mitigation
  ports:
    - name: http
      port: 8081
      targetPort: 8081
```

- [ ] **Step 4: Lint and verify**

```bash
helm lint charts/bess-upf/charts/mitigation
helm template charts/bess-upf/charts/mitigation | kubectl apply --dry-run=client -f -
```
Expected: lint passes, dry-run succeeds.

- [ ] **Step 5: Commit**

```bash
git add charts/bess-upf/charts/mitigation
git commit -m "feat(k8s): mitigation subchart — 2-replica Deployment + Service"
```

---

## Task 8: Ingress — route to serve and mitigation only

**Files:**
- Create: `charts/bess-upf/templates/ingress.yaml`

- [ ] **Step 1: Write the Ingress resource**

`charts/bess-upf/templates/ingress.yaml`:
```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: bess-upf
  namespace: {{ .Values.global.namespace }}
  annotations:
    nginx.ingress.kubernetes.io/rewrite-target: /$2
spec:
  ingressClassName: nginx
  rules:
    - http:
        paths:
          - path: /api/serve(/|$)(.*)
            pathType: ImplementationSpecific
            backend:
              service:
                name: serve
                port:
                  number: 8000
          - path: /api/mitigation(/|$)(.*)
            pathType: ImplementationSpecific
            backend:
              service:
                name: mitigation
                port:
                  number: 8081
```
This lives in the umbrella chart's own `templates/`, not a subchart, since it references Services owned by two different subcharts (`serve`, `mitigation`).

- [ ] **Step 2: Verify template renders (full dependency graph now needed)**

```bash
helm dependency build charts/bess-upf
helm template charts/bess-upf | kubectl apply --dry-run=client -f -
```
Expected: dry-run succeeds for the full resource set across all 6 charts + the new Ingress.

- [ ] **Step 3: Commit**

```bash
git add charts/bess-upf/templates/ingress.yaml
git commit -m "feat(k8s): ingress-nginx routes for serve and mitigation HTTP APIs"
```

---

## Task 9: Full install + functional parity check

**Files:** none new — this task exercises Tasks 0–8's output.

- [ ] **Step 1: Install the full stack**

```bash
helm install bess-upf charts/bess-upf -n bess-upf --create-namespace
```

- [ ] **Step 2: Wait for readiness and verify pod count**

```bash
kubectl wait --for=condition=Ready pod --all -n bess-upf --timeout=300s
kubectl get pods -n bess-upf -o wide
```
Expected: 3 redpanda pods, 3 clickhouse pods, 1 ray head + 2 ray workers, 2 pipeline pods, 2 mitigation pods, 1 redis pod, 1 kuberay-operator pod, ingress-nginx controller pod — all `Running`/`Ready`.

- [ ] **Step 3: Run existing test suites against the in-cluster stack**

```bash
kubectl port-forward -n bess-upf svc/clickhouse 8123:8123 &
kubectl port-forward -n bess-upf svc/redpanda 9092:9092 &
kubectl port-forward -n bess-upf svc/mitigation 8081:8081 &
kubectl port-forward -n bess-upf svc/serve 8000:8000 &

cd pipeline && KAFKA_BROKER=localhost:9092 CLICKHOUSE_HOST=localhost python -m pytest tests/ -v
cd ../mitigation && KAFKA_BROKER=localhost:9092 API_URL=http://localhost:8081 python -m pytest tests/ -v
cd ../serve && RAY_SERVE_URL=http://localhost:8000 python -m pytest tests/ -v
```
Expected: same pass counts as the Docker Compose baseline (46/46 mitigation, existing pipeline/serve counts) — confirms no regression from the platform change alone.

- [ ] **Step 4: Kill the port-forwards**

```bash
kill %1 %2 %3 %4 2>/dev/null || true
```

No commit — this task is a verification run, not a code change.

---

## Task 10: Node-loss test — the Phase 4a exit criterion

**Files:**
- Create: `scripts/k8s-node-loss-test.sh`
- Create: `docs/runbooks/phase4a-node-loss-test.md`

- [ ] **Step 1: Write the node-loss test script**

`scripts/k8s-node-loss-test.sh`:
```bash
#!/bin/bash
# Usage: ./scripts/k8s-node-loss-test.sh <component>
# component: redpanda | clickhouse | ray-head
set -euo pipefail

COMPONENT="${1:?usage: $0 <redpanda|clickhouse|ray-head>}"
NAMESPACE=bess-upf

case "$COMPONENT" in
  redpanda)  LABEL="app=redpanda" ;;
  clickhouse) LABEL="app=clickhouse" ;;
  ray-head)  LABEL="ray.io/node-type=head" ;;
  *) echo "unknown component: $COMPONENT" >&2; exit 1 ;;
esac

echo "== Writing canary record for $COMPONENT =="
CANARY_ID="canary-$(date +%s)"
kubectl exec -n "$NAMESPACE" clickhouse-0 -- clickhouse-client --query \
  "INSERT INTO bess_upf.action_audit (action_id, upf_id, ts, action_class, trust_level, dry_run, success, message, anomaly_score, model_version, rollback_token) \
   VALUES ('${CANARY_ID}', 'canary-upf', now64(3), 'CANARY', 'NONE', 1, 1, 'node-loss-test canary', 0.0, 'test', '')"

echo "== Identifying node hosting a $COMPONENT pod =="
POD=$(kubectl get pods -n "$NAMESPACE" -l "$LABEL" -o jsonpath='{.items[0].metadata.name}')
NODE=$(kubectl get pod -n "$NAMESPACE" "$POD" -o jsonpath='{.spec.nodeName}')
echo "Target pod: $POD on node: $NODE"

echo "== Stopping node $NODE (docker stop) =="
docker stop "$NODE"

echo "== Waiting for k8s to mark pod NotReady and reschedule =="
sleep 15
kubectl get pods -n "$NAMESPACE" -o wide | grep -E "$COMPONENT|NAME"

echo "== Verifying canary record still readable from a surviving replica =="
SURVIVOR=$(kubectl get pods -n "$NAMESPACE" -l "$LABEL" -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n "$NAMESPACE" "$SURVIVOR" -- clickhouse-client --query \
  "SELECT count() FROM bess_upf.action_audit WHERE action_id = '${CANARY_ID}'"

echo "== Restoring node $NODE =="
docker start "$NODE"

echo "== Waiting for rescheduled pod to rejoin =="
sleep 30
kubectl get pods -n "$NAMESPACE" -o wide | grep -E "$COMPONENT|NAME"

echo "Done. Record pass/fail in docs/runbooks/phase4a-node-loss-test.md"
```

- [ ] **Step 2: Write the runbook with a results table to fill in**

`docs/runbooks/phase4a-node-loss-test.md`:
```markdown
# Phase 4a Node-Loss Test Runbook

Run once per stateful/critical component. Restore the node (`docker start`) between runs before starting the next one.

## Procedure (per component)

```bash
./scripts/k8s-node-loss-test.sh redpanda
./scripts/k8s-node-loss-test.sh clickhouse
./scripts/k8s-node-loss-test.sh ray-head
```

For `redpanda` and `clickhouse`, also confirm via `kubectl logs` that the rescheduled replica's log shows it rejoining quorum (Redpanda: `rpk cluster health` reports the node back as `Healthy: true`; ClickHouse: `system.zookeeper` shows the replica reconnected, `system.replicas` shows `is_readonly = 0` again).

For `ray-head`, additionally confirm the Serve endpoint (`curl http://localhost:8000/`) resumes serving without a `helm upgrade` — the operator should reschedule the head pod and it should recover GCS state from Redis automatically.

`pipeline` and `mitigation` don't need this test — they're stateless-at-this-layer Deployments (2 replicas each), and losing one replica's node is just normal k8s rescheduling, not a coordination question.

## Results

| Component  | Surviving replicas kept serving | Pod rescheduled | Replica resynced/rejoined quorum | Canary record intact after recovery | Pass/Fail |
|------------|----------------------------------|------------------|-----------------------------------|--------------------------------------|-----------|
| redpanda   |                                  |                  |                                    |                                       |           |
| clickhouse |                                  |                  |                                    |                                       |           |
| ray (head) |                                  |                  |                                    |                                       |           |

Fill in after running each test. All three rows must be Pass for Phase 4a's exit criterion ("survives node loss on test cluster") to be met.
```

- [ ] **Step 3: Run all three tests and fill in the results table**

```bash
chmod +x scripts/k8s-node-loss-test.sh
./scripts/k8s-node-loss-test.sh redpanda
./scripts/k8s-node-loss-test.sh clickhouse
./scripts/k8s-node-loss-test.sh ray-head
```
Fill `docs/runbooks/phase4a-node-loss-test.md`'s table with the observed results for each run.

- [ ] **Step 4: Commit**

```bash
git add scripts/k8s-node-loss-test.sh docs/runbooks/phase4a-node-loss-test.md
git commit -m "test(k8s): node-loss test script + runbook — Phase 4a exit-criterion evidence"
```

---

## Self-Review

**Spec coverage:**
- kind 1+3 nodes → Task 0. ✓
- Namespace `bess-upf` → Task 0. ✓
- GHCR + imagePullSecrets → Tasks 0, 1. ✓
- Umbrella chart, 6 logical subcharts → Tasks 2–8 (mechanism deviation #2 noted above). ✓
- redpanda StatefulSet/RF3/PDB → Task 3. ✓
- clickhouse StatefulSet/Keeper/ReplicatedMergeTree → Task 4. ✓
- serve KubeRay/RayCluster/Redis GCS FT → Task 5. ✓
- pipeline/mitigation plain Deployments, 2 replicas → Tasks 6, 7. ✓
- ingress-nginx NodePort, Caddyfile routes → Ingress → Task 8 (backend deviation #1 noted above). ✓
- Verification: install, pytest parity, node-loss test per component, Ray head test, pass/fail record → Tasks 9, 10. ✓
- Out-of-scope items (mTLS/RBAC/secrets, DR/SLOs, real certs) — correctly not present in any task. ✓

**Placeholder scan:** no TBD/TODO, no "add appropriate X" — every step has literal file content or literal commands.

**Type/name consistency check:** Service names used consistently across tasks — `redpanda` (client) / `redpanda-headless` (peer discovery), `clickhouse` (client) / `clickhouse-headless` (peer discovery), `serve` (Ray head HTTP), `mitigation`. Env vars `KAFKA_BROKER=redpanda:9092` and `RAY_SERVE_URL=http://serve:8000` in Task 6 match the Service names defined in Tasks 3 and 5. Topics created in Task 3's Job (`upf.metrics.raw`, `upf.anomalies.critical`, `upf.shadow.detections`, `upf.action_audit`) match the ones consumed in Task 4's `init.sql` Kafka-engine tables.
