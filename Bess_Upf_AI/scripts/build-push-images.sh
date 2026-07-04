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
