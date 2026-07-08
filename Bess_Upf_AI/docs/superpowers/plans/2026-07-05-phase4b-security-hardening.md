# Phase 4b: Security Hardening

## Overview
This phase builds on the Kubernetes/Helm baseline from Phase 4a by adding essential carrier-grade security constraints. It achieves this without changing the application logic, purely focusing on platform-level hardening.

## Completed Security Additions
1. **mTLS via Linkerd:**
   - Linkerd sidecars injected automatically into the `bess-upf` namespace via the `linkerd.io/inject: enabled` annotation in pod templates (toggled via `global.security.mtls.enabled`).
   - Ensures all intra-cluster communication (Ray workers, Kafka brokers, ClickHouse keeper) is encrypted and mutually authenticated.

2. **RBAC (Least Privilege):**
   - Each stateful and stateless service (`redpanda`, `clickhouse`, `serve`, `mitigation`, `pipeline`) now runs with its own dedicated `ServiceAccount` instead of defaulting to the namespace's default account.
   - `automountServiceAccountToken: false` is set for services that do not need to speak to the Kubernetes API, massively reducing the blast radius of a pod compromise.

3. **Network Policies:**
   - A `default-deny-ingress` NetworkPolicy isolates the `bess-upf` namespace.
   - Explicit ingress rules allow intra-namespace communication and permit external traffic strictly through the `ingress-nginx` controller.

4. **Cert-Manager Integration:**
   - The ingress configuration is annotated to dynamically request real TLS certificates via `cert-manager.io/cluster-issuer: selfsigned-issuer`.

## Outstanding Platform Setup (Runbook)
Before deploying `helm install bess-upf ./charts/bess-upf`, the following platform dependencies must be installed:

### 1. Install Linkerd
```bash
linkerd install --crds | kubectl apply -f -
linkerd install | kubectl apply -f -
linkerd check
```

### 2. Install Cert-Manager
```bash
helm repo add jetstack https://charts.jetstack.io
helm install cert-manager jetstack/cert-manager --namespace cert-manager --create-namespace --set installCRDs=true
# Create a SelfSigned issuer for local dev
kubectl apply -f - <<EOF
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: selfsigned-issuer
spec:
  selfSigned: {}
EOF
```

### 3. Install Sealed Secrets
```bash
helm repo add sealed-secrets https://bitnami-labs.github.io/sealed-secrets
helm install sealed-secrets sealed-secrets/sealed-secrets -n kube-system
```

### 4. Encrypting the `ghcr-pull-secret`
Instead of using plain `kubernetes.io/dockerconfigjson` secrets, encrypt the pull secret locally using `kubeseal` and commit the `SealedSecret` yaml to the repo.
```bash
kubectl create secret docker-registry ghcr-pull-secret \
    --docker-server=ghcr.io \
    --docker-username=YOUR_USER \
    --docker-password=YOUR_PAT \
    --namespace bess-upf --dry-run=client -o yaml | kubeseal \
    --controller-name=sealed-secrets \
    --controller-namespace=kube-system \
    --format yaml > charts/bess-upf/templates/ghcr-pull-secret-sealed.yaml
```
