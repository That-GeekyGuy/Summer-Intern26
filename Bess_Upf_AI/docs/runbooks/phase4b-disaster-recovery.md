# Phase 4b: Disaster Recovery Runbook

This runbook outlines the procedures for recovering the BESS-UPF v2 components from catastrophic node failures in the Kubernetes cluster.

## 1. ClickHouse Recovery

ClickHouse is deployed as a 3-node StatefulSet using `ReplicatedMergeTree` and ClickHouse Keeper.

### Scenario A: Single Node Failure
**Impact**: Zero downtime. The cluster will continue to accept reads and writes if 2/3 nodes are up (quorum).
**Recovery**:
1. Kubernetes will automatically reschedule the failed pod.
2. Upon startup, the new pod will resync data from the other replicas via Keeper.
3. Check sync status:
   `kubectl exec clickhouse-0 -n bess-upf -- clickhouse-client --query "SELECT * FROM system.replication_queue"`

### Scenario B: Total Cluster Failure (All 3 nodes down)
**Impact**: Complete data ingestion halt.
**Recovery**:
1. If PVCs are intact, restarting the pods will recover the state.
2. Wait for Keeper to establish quorum. Do not force-restart pods repeatedly, as Keeper needs to perform leader election.
3. If Keeper state is corrupted, refer to the ClickHouse manual for Keeper recovery (involves deleting the `coordination/log` dir on followers and letting the leader replicate).

## 2. Redpanda (Kafka) Recovery

Redpanda is deployed with 3 replicas. Topics are configured with `replication-factor: 3`.

### Scenario A: Single Node Failure
**Impact**: Zero data loss. Partitions with leaders on the failed node will elect new leaders.
**Recovery**:
1. Kubernetes reschedules the pod.
2. Redpanda performs partition reassignment and catches up on missing segments.
3. Check cluster health:
   `kubectl exec redpanda-0 -n bess-upf -- rpk cluster health`

## 3. Ray (Serve) Recovery

Ray Serve is deployed via KubeRay Operator with 1 head node and multiple workers.

### Scenario A: Worker Node Failure
**Impact**: Slight latency spike for inference requests.
**Recovery**:
1. Ray automatically routes traffic to healthy workers.
2. KubeRay provisions a new worker pod.

### Scenario B: Head Node Failure
**Impact**: Inference requests (if routed through the head node ingress) will fail until the head node restarts.
**Recovery**:
1. KubeRay operator automatically detects the head node failure and recreates the pod.
2. GCS fault tolerance (enabled via Redis) ensures the cluster state (actors, objects) is recovered.
3. Check Ray status:
   `kubectl exec -it <head-pod> -n bess-upf -- ray status`
