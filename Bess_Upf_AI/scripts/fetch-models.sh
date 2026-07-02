#!/usr/bin/env bash
# Fetch model artifacts from MinIO instead of Git LFS.
# Uses docker run minio/mc so mc doesn't need to be installed on the host;
# MinIO's S3 port (9000) is internal-only — reached via the bess-upf-backend network.
#
# Usage: bash scripts/fetch-models.sh [dest-dir]
#   dest-dir defaults to "models" (relative to repo root).
#
# Requires: docker, stack up (minio container healthy), MINIO_ROOT_USER/PASSWORD in env or .env.
set -euo pipefail

DEST="${1:-models}"
BUCKET="${MODEL_BUCKET:-bess-models}"
NETWORK="bess-upf-backend"

# Load creds from .env if not already in environment.
if [[ -z "${MINIO_ROOT_USER:-}" ]] && [[ -f .env ]]; then
    # shellcheck disable=SC2046
    export $(grep -E '^MINIO_ROOT_(USER|PASSWORD)=' .env | xargs)
fi

: "${MINIO_ROOT_USER:?MINIO_ROOT_USER not set — check .env}"
: "${MINIO_ROOT_PASSWORD:?MINIO_ROOT_PASSWORD not set — check .env}"

mkdir -p "$DEST" tools/train/data

echo "[fetch-models] Fetching from MinIO bucket '${BUCKET}' → ${DEST}/ ..."

docker run --rm \
    --network "$NETWORK" \
    -v "$(pwd)/${DEST}:/dest" \
    -v "$(pwd)/tools/train/data:/train-data" \
    -e MC_HOST_modelsrc="http://${MINIO_ROOT_USER}:${MINIO_ROOT_PASSWORD}@minio:9000" \
    minio/mc:latest \
    sh -c "
        mc cp --recursive modelsrc/${BUCKET}/models/ /dest/ 2>/dev/null || true
        mc cp --recursive modelsrc/${BUCKET}/train-data/ /train-data/ 2>/dev/null || true
        echo '[fetch-models] Done.'
    "

echo "[fetch-models] Artifacts written to ${DEST}/ and tools/train/data/."
