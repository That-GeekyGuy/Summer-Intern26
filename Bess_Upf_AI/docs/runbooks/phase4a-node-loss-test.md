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

## Results (2026-07-05, fresh kind cluster)

| Component  | Surviving replicas kept serving | Pod rescheduled | Replica resynced/rejoined quorum | Canary record intact after recovery | Pass/Fail |
|------------|----------------------------------|------------------|-----------------------------------|--------------------------------------|-----------|
| redpanda   | N/A — never reached RF=3         | N/A              | No                                 | N/A                                   | **Blocked** — 3-node raft/RPC join never succeeds; see below |
| clickhouse | Yes — clickhouse-1 served reads throughout | Recovered in place (local-path storage ties the pod to its node, doesn't migrate) | Yes — table count/data confirmed intact on all 3 nodes after recovery | Yes — canary readable via clickhouse-1 during outage, still present after clickhouse-0 recovered | **Pass** |
| ray (head) | N/A (singleton, no replica)      | Recovered in place (same local-path/hostPath binding as clickhouse) | N/A | N/A — see note below | **Pass on Ray runtime recovery; Serve app itself was never actually running — separate gap, see below** |

### redpanda: blocked, not a config issue

Every plausible cause was tested and ruled out with direct evidence, across two Redpanda versions (v23.2.19 and v24.1.1) and multiple pod-management strategies:
- DNS resolution (both `getent` and redpanda's internal `c-ares`) — confirmed working in both directions once pods are up.
- Bidirectional raw TCP connectivity on the raft/RPC port 33145 — confirmed working both directions, including an 8KB payload (rules out MTU).
- Seeds configuration — tried single-seed and all-seeds, in combination with both `Parallel` and `OrderedReady` pod management (4 total combinations tested).
- Redpanda version — identical `rpc::errc::missing_node_rpc_client` / "no leader controller elected" symptom on v23.2.19 and v24.1.1.

The single-seed + `OrderedReady` combination (the currently-shipped config, and the standard/documented pattern) gets furthest: node 0 bootstraps cleanly and stays healthy, but every subsequent node gets stuck as a permanent raft learner, never promoted to voter, despite node 0's own broker registry showing the correct address for the joining node. This looks like an internal Redpanda raft/RPC protocol issue specific to this environment (nested kind cluster on Docker Desktop/WSL2), not a chart misconfiguration. Recommended next steps: file/search Redpanda GitHub issues with this exact log signature, try `rpk debug` diagnostics bundle, or consider Strimzi/Kafka as an alternative if Redpanda's k8s clustering story doesn't resolve.

### ray-head: Ray runtime HA works, but the Serve app was never deployed

The RayCluster's head pod does recover from node loss (confirmed: recovers in place, Ray runtime restarts cleanly, GCS via Redis works as designed). However, testing the actual `/` HTTP endpoint after recovery revealed **connection refused on port 8000** — checking the pod logs showed only the base `ray start ... --block` command running. KubeRay's operator injects and controls the head/worker container's actual startup command itself; `serve/Dockerfile`'s own `CMD ["serve", "run", "app:app_node", ...]` never executes inside a KubeRay-managed pod. The RayCluster is up and Ray-healthy, but the actual Serve application (`app.py`) has never been deployed to it in this chart — a separate, real gap from the node-loss question. Needs either a `RayService` CR (KubeRay's purpose-built resource for exactly this — deploys + manages the Serve app declaratively) or a post-install Job that runs `serve deploy`/`serve run` against the running cluster, similar to the topic-init/schema-init Job pattern used elsewhere in this chart.

**Exit criterion status:** 2 of 3 components pass their node-loss test. redpanda's RF=3 HA is not yet achieved (single-node only, blocked on an unresolved raft/RPC issue). The ray-head recovery mechanism itself works, but there's no actual Serve application running to verify end-to-end inference resumes.
