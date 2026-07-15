# RUNBOOK — CoreWatch (v2 Kubernetes Stack)

Quick reference for managing the Kubernetes-native CoreWatch streaming stack
(Redpanda, ClickHouse, Ray/KubeRay). Bring the whole stack up or redeploy
with `python install.py` (see [README.md](./README.md)) — don't
`kubectl patch`/`kubectl apply` live resources directly; see §5.

---

## 1. ClickHouse Disaster Recovery (Backup & Restore)

ClickHouse is the single source of truth for metrics, anomalies, and audit
logs. It runs as a 3-replica `ReplicatedMergeTree` StatefulSet — losing one
node is handled automatically via quorum. A full backup is only needed for
catastrophic cluster-wide failure.

```bash
kubectl exec -it -n bess-upf clickhouse-0 -- clickhouse-client

-- Backup
BACKUP DATABASE default TO Disk('backups', 'backup_2026_07_08/');

-- Restore (drop first if corrupted)
DROP TABLE default.action_audit;
RESTORE DATABASE default FROM Disk('backups', 'backup_2026_07_08/');
```
*Assumes a `backups` disk is configured in ClickHouse's config pointing at a PV/S3.*

---

## 2. Redpanda Recovery

### 2.1 Pod loss (routine)
```bash
kubectl exec -it -n bess-upf redpanda-0 -- rpk cluster health
```
A single pod restart resyncs from the raft quorum automatically.

### 2.2 A node won't join / `nodes_down` / `missing_node_rpc_client`
This has bitten us twice this way — check both before assuming it's a new bug:

1. **c-ares DNS bug.** c-ares (redpanda's resolver) tries `ndots:5`
   search-domain expansion even for names with enough dots, unless the
   FQDN ends in a literal `.`. Check every redpanda pod's args
   (`kubectl get statefulset redpanda -n bess-upf -o jsonpath='{.spec.template.spec.containers[0].args}'`)
   — `--seeds` and `--advertise-rpc-addr` must both end in `.`, **including
   node 0** (the seed/leader) — a stale node 0 that predates the fix will
   silently break nodes 1/2 even though *their* args are correct.
2. **One-shot Hello handshake.** If a node was recreated (e.g. you deleted
   its pod), the leader's Hello handshake to it fires once and does not
   retry automatically. If a peer never converges after a few minutes with
   correct args on all sides, `kubectl delete pod redpanda-<n> -n bess-upf`
   to force a fresh join attempt — this alone has resolved it every time
   we've hit it.

### 2.3 Total quorum loss (catastrophic)
```bash
python install.py --skip-build   # reconciles the StatefulSet + re-runs topic init
```
Since Redpanda here holds transient stream data (no Tiered Storage), it
backfills from the UPF simulator/real UPF once topics exist again.

---

## 3. Ray Serve & ML Recovery

### 3.1 Head node failure
KubeRay operator restarts a fresh `ray-head` pod automatically; GCS fault
tolerance via `ray-gcs-redis` lets it resume without dropping workers.

### 3.2 RayCluster stuck / `ImagePullBackOff`
Almost always image drift, not a KubeRay bug:
```bash
kubectl get raycluster bess-upf-serve -n bess-upf -o jsonpath='{.spec.headGroupSpec.template.spec.containers[0].image}'
```
Compare against `charts/bess-upf/charts/serve/values.yaml`'s `tag`. If it
doesn't match, something was `kubectl patch`'d directly — see §5. The
chart uses `imagePullPolicy: IfNotPresent` deliberately: local/kind images
are built + `kind load`-ed, never pulled from a real registry; `Always`
will try to reach the registry anyway and fail/hang even when the correct
image is already cached on the node.

### 3.3 `deploy-ml-models` Job crash-loops
Check `kubectl logs -n bess-upf -l job-name=deploy-ml-models`. If it can't
find `/app/deploy_models.py`, the deployed image is stale — `serve/Dockerfile`'s
`COPY` line must include `deploy_models.py`; rebuild with `python install.py`.

### 3.4 Total cluster loss (Redis data loss)
Ray cluster state is wiped; KubeRay starts fresh and models must be
redeployed — this happens automatically via the Helm post-install/upgrade
hook on the next `python install.py`.

---

## 4. vLLM / LLM Chat

Real mode (`vllm/vllm-openai` + a quantized Qwen model) needs an actual GPU
node — it will crash-loop on a CPU-only kind cluster (CUDA-only image) and
even with GPU access the HF cache is an `emptyDir` (wiped every restart),
so a fresh download happens on every pod restart unless backed by a PVC.

For local dev / CPU-only clusters, use mock mode (default):
```yaml
# charts/bess-upf/charts/analysis/values.yaml
vllm:
  mock:
    enabled: true   # canned responses, no GPU/model needed
```
Only flip this to `false` on a node pool with real GPU capacity, and back
the HF cache with a PVC instead of `emptyDir` first.

---

## 5. Don't hand-patch the cluster

`kubectl patch` / `kubectl apply` / `kubectl set image` on live resources
bypasses Helm's ownership tracking. Once Helm and the live cluster
disagree about a field, the *next* `helm upgrade` fails with a
field-manager conflict (`Apply failed with N conflicts: conflicts with
"kubectl-patch"/"kubectl-client-side-apply"`), and the release gets stuck
in `failed` status. We hit this simultaneously across ingress-nginx probes,
ClickHouse's `volumeClaimTemplates`, Redpanda's start args, and the
RayCluster's image — all from ad-hoc debugging patches that were never
folded back into `values.yaml`.

**Always change `values.yaml`/templates, then `helm upgrade`.** If drift
already happened:
```bash
helm upgrade bess-upf charts/bess-upf -n bess-upf --force-conflicts
```
If that still fails (e.g. an immutable field like a Job's pod template, or
the cluster's CNI is in a bad state — see §6), the reliable fix is a clean
`kind delete cluster --name bess-upf && python install.py`.

---

## 6. Kubernetes / kind Troubleshooting

### 6.1 Pods stuck Pending
```bash
kubectl get pods -n bess-upf -w
kubectl describe pod <pod-name> -n bess-upf
```
Check node resources/taints.

### 6.2 Cross-node pod networking broken after a host/Docker Desktop restart
Symptom: same-node pod traffic works (kubelet probes pass), but cross-node
pod-to-pod TCP fails entirely (e.g. Helm's admission webhook calls time
out, redpanda/serve peers can't reach each other) even though `ip route`
looks correct on every node. This is a kind-on-Docker-Desktop/WSL2 overlay
issue, not an app config problem.
1. Restart Docker Desktop — fixes node-level (docker-IP) connectivity.
2. If pod-to-pod (10.244.x.x) traffic is still broken afterward, don't
   chase it further at the iptables/CNI layer — `kind delete cluster
   --name bess-upf && python install.py` resets the overlay cleanly and is
   faster than debugging it.

### 6.3 Helm release stuck in `failed`
See §5.
