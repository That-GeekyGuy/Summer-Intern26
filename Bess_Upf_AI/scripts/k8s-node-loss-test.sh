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

CH_PASSWORD=$(kubectl get secret -n "$NAMESPACE" clickhouse-credentials -o jsonpath='{.data.password}' | base64 -d)
CH_AUTH="--user chuser --password ${CH_PASSWORD}"

echo "== Writing canary record for $COMPONENT =="
CANARY_ID="canary-$(date +%s)"
kubectl exec -n "$NAMESPACE" clickhouse-0 -- clickhouse-client $CH_AUTH --query \
  "INSERT INTO bess_upf.action_audit (action_id, upf_id, ts, action_class, trust_level, dry_run, success, message, anomaly_score, model_version, rollback_token) \
   VALUES ('${CANARY_ID}', 'canary-upf', now64(3), 'CANARY', 'NONE', 1, 1, 'node-loss-test canary', 0.0, 'test', '')"

echo "== Identifying node hosting a $COMPONENT pod =="
POD=$(kubectl get pods -n "$NAMESPACE" -l "$LABEL" -o jsonpath='{.items[0].metadata.name}')
NODE=$(kubectl get pod -n "$NAMESPACE" "$POD" -o jsonpath='{.spec.nodeName}')
echo "Target pod: $POD on node: $NODE"

echo "== Stopping node $NODE (docker stop) =="
docker stop "$NODE"

echo "== Waiting for k8s to mark pod NotReady and reschedule =="
# node-monitor-grace-period defaults to 40s before kubelet is marked NotReady,
# plus further pod-eviction delay — 15s was not enough, confirmed by the
# survivor-selection below still picking a pod on the just-stopped node.
sleep 60
kubectl get pods -n "$NAMESPACE" -o wide | grep -E "$COMPONENT|NAME"

echo "== Verifying canary record still readable from a surviving replica =="
# Exclude pods still scheduled on the stopped node — jsonpath alone can't
# filter by nodeName, so pick the first pod NOT on $NODE via a small loop.
SURVIVOR=""
for pod in $(kubectl get pods -n "$NAMESPACE" -l "$LABEL" -o jsonpath='{.items[*].metadata.name}'); do
  pod_node=$(kubectl get pod -n "$NAMESPACE" "$pod" -o jsonpath='{.spec.nodeName}')
  if [ "$pod_node" != "$NODE" ]; then
    SURVIVOR="$pod"
    break
  fi
done
echo "Survivor pod: $SURVIVOR"
kubectl exec -n "$NAMESPACE" "$SURVIVOR" -- clickhouse-client $CH_AUTH --query \
  "SELECT count() FROM bess_upf.action_audit WHERE action_id = '${CANARY_ID}'"

echo "== Restoring node $NODE =="
docker start "$NODE"

echo "== Waiting for rescheduled pod to rejoin =="
sleep 30
kubectl get pods -n "$NAMESPACE" -o wide | grep -E "$COMPONENT|NAME"

echo "Done. Record pass/fail in docs/runbooks/phase4a-node-loss-test.md"
