#!/usr/bin/env sh
# MinIO bootstrap — runs once in the minio-init container.
# Creates the export bucket and a least-privilege service account for the
# export job (read + write to EXPORT_BUCKET only; no admin access).
#
# Re-running this script is safe: mc commands are idempotent.
set -eu

ALIAS="local"
ENDPOINT="http://minio:9000"

echo "[minio-init] Waiting for MinIO to accept connections..."
until mc alias set "${ALIAS}" "${ENDPOINT}" \
        "${MINIO_ROOT_USER}" "${MINIO_ROOT_PASSWORD}" > /dev/null 2>&1; do
    sleep 2
done
echo "[minio-init] Connected."

# ── Create export bucket ──────────────────────────────────────────────────────
echo "[minio-init] Creating bucket: ${EXPORT_BUCKET}"
mc mb --ignore-existing "${ALIAS}/${EXPORT_BUCKET}"

# Recommended production lifecycle policy (not enforced here — apply via
# MinIO console or mc ilm after validating your retention needs):
#
#   Transition to GLACIER-equivalent after 90 days.
#   Expire (delete) objects after 365 days (or longer if regulatory holds apply).
#   Versioning OFF for this bucket — the export job uses overwrite semantics
#   for idempotency via manifest files; versioning adds storage cost without benefit.
#
# To apply a basic expiry rule:
#   mc ilm add --expiry-days 365 local/${EXPORT_BUCKET}

# ── Create least-privilege service account ────────────────────────────────────
# The export job only needs s3:GetObject, s3:PutObject, and s3:ListBucket on
# EXPORT_BUCKET. We create a dedicated policy and bind it to a service account.
echo "[minio-init] Creating export-job policy..."

POLICY_NAME="upf-exporter-policy"
POLICY_JSON=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::${EXPORT_BUCKET}",
        "arn:aws:s3:::${EXPORT_BUCKET}/*"
      ]
    }
  ]
}
EOF
)

# Write policy to a temp file (mc admin policy create requires a file path).
POLICY_FILE="/tmp/${POLICY_NAME}.json"
printf '%s' "${POLICY_JSON}" > "${POLICY_FILE}"

mc admin policy create "${ALIAS}" "${POLICY_NAME}" "${POLICY_FILE}" || \
    echo "[minio-init] Policy already exists, skipping."

# ── Create or update the exporter service user ───────────────────────────────
echo "[minio-init] Creating service account: ${MINIO_EXPORTER_USER}"

# mc admin user add is idempotent if user already exists (returns error we swallow).
mc admin user add "${ALIAS}" "${MINIO_EXPORTER_USER}" "${MINIO_EXPORTER_PASSWORD}" || \
    echo "[minio-init] User already exists, skipping creation."

mc admin policy attach "${ALIAS}" "${POLICY_NAME}" \
    --user "${MINIO_EXPORTER_USER}" || \
    echo "[minio-init] Policy already attached."

echo "[minio-init] Bootstrap complete."
echo "[minio-init]   Bucket : ${EXPORT_BUCKET}"
echo "[minio-init]   User   : ${MINIO_EXPORTER_USER}"
echo "[minio-init]   Policy : ${POLICY_NAME} (GetObject, PutObject, DeleteObject, ListBucket)"
