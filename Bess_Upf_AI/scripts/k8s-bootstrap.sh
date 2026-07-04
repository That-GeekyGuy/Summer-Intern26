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
