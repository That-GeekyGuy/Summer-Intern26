# RUNBOOK — BESS-UPF AI (v2 Kubernetes Stack)

Quick reference for on-call and ops to manage the Kubernetes-native BESS-UPF streaming stack. 

*Note: This runbook covers the Phase 4 Kubernetes architecture (ClickHouse, Redpanda, Ray). Legacy v1 (Prometheus/VictoriaMetrics/Docker Compose) runbooks have been archived.*

---

## 1. ClickHouse Disaster Recovery (Backup & Restore)

ClickHouse acts as the single source of truth for all historical metrics, anomalies, and audit logs. Since the platform utilizes `ReplicatedMergeTree` alongside ClickHouse Keeper, the loss of a single node is handled automatically via quorum recovery. A full backup is only required for catastrophic cluster-wide failure.

### 1.1 Taking a Backup

Execute ClickHouse's native `BACKUP` command to export data to remote S3 storage or a mounted volume.

```bash
# Connect to the ClickHouse pod
kubectl exec -it -n bess-upf sts/clickhouse -- clickhouse-client

# Execute backup for the default database to a mounted backup disk
BACKUP DATABASE default TO Disk('backups', 'backup_2026_07_06/');
```
*Note: This assumes the `backups` disk is configured in ClickHouse's `config.xml` to point to an S3 bucket or PV.*

### 1.2 Restoring from Backup

To recover from a catastrophic failure or data corruption:
```bash
# Drop corrupted tables if they exist
DROP TABLE default.action_audit;
DROP TABLE default.shadow_detections;

# Restore from the backup location
RESTORE DATABASE default FROM Disk('backups', 'backup_2026_07_06/');
```

---

## 2. Redpanda Recovery

Redpanda holds transient stream data (e.g., `upf.metrics.raw`, `upf.anomalies.critical`). The data is ephemeral unless Tiered Storage is explicitly enabled in production.

### 2.1 Pod or Node Loss
Redpanda runs as a 3-replica `StatefulSet` with raft-based replication. If one pod goes down, it will automatically resync its partitions from the remaining quorum when rescheduled.
```bash
# Check cluster health
kubectl exec -it -n bess-upf redpanda-0 -- rpk cluster health
```

### 2.2 Total Quorum Loss (Catastrophic)
If all 3 nodes are lost and PVCs are destroyed, you will need to re-initialize the topics:
```bash
# Run the helm release again to recreate the StatefulSet
helm upgrade --install bess-upf ./charts/bess-upf -n bess-upf

# Re-run the topic initialization job
kubectl delete job redpanda-topic-init -n bess-upf
helm upgrade bess-upf ./charts/bess-upf -n bess-upf
```
Since the system is a streaming engine, it will quickly backfill new data directly from the UPF simulators.

---

## 3. Ray Serve & ML Recovery

Ray Serve hosts the MOMENT and sklearn inference models. It uses a single Redis instance as its Global Control Store (GCS) to provide Head Node fault tolerance.

### 3.1 Head Node Failure
If the `ray-head` pod crashes or the underlying node dies, the KubeRay operator will automatically spin up a new head pod. It will fetch the cluster state from the external `ray-gcs-redis` service and seamlessly resume without dropping worker pods.

### 3.2 Total Cluster Loss (Redis Data Loss)
If the Redis instance is also lost, the Ray cluster state is wiped.
1. The KubeRay operator will start a fresh cluster.
2. The deployed models will be lost and must be resubmitted.
```bash
# Retrieve the Ray Cluster head service IP
export RAY_ADDRESS=http://$(kubectl get svc -n bess-upf -l ray.io/node-type=head -o jsonpath='{.items[0].spec.clusterIP}'):8265

# Resubmit the models (e.g. from your CI/CD pipeline or local script)
ray job submit --working-dir ./serve -- python deploy_models.py
```

---

## 4. Kubernetes Troubleshooting

### 4.1 Pods Stuck in Pending
Check if the cluster has enough resources or if a Node selector/taint is preventing scheduling.
```bash
kubectl get pods -n bess-upf -w
kubectl describe pod <pod-name> -n bess-upf
```

### 4.2 Network Policy / Connection Refused
If `pipeline` cannot reach `clickhouse` or `mitigation` cannot reach `redpanda`, verify that `NetworkPolicies` are allowing traffic.
```bash
# Check if policies are applied
kubectl get networkpolicies -n bess-upf

# Temporarily delete the default-deny to isolate the issue
kubectl delete networkpolicy default-deny-ingress -n bess-upf
```

### 4.3 Cert-Manager & TLS Issues
If `https://bess-upf.local` shows an invalid certificate, verify cert-manager is issuing the cert correctly.
```bash
kubectl get certificates -n bess-upf
kubectl describe certificate bess-upf-tls -n bess-upf
```
