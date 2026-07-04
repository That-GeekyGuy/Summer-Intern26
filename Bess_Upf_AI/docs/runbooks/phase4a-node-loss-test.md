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
