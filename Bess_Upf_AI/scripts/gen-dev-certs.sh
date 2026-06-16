#!/usr/bin/env sh
# ╔══════════════════════════════════════════════════════════════════════╗
# ║  DEV ONLY — self-signed TLS certificate generator                   ║
# ║  DO NOT use these certificates in production.                        ║
# ║  For production, remove the `tls` block from the Caddyfile and      ║
# ║  let Caddy obtain a real cert via ACME (Let's Encrypt).              ║
# ╚══════════════════════════════════════════════════════════════════════╝
set -eu

CERTS_DIR="$(dirname "$0")/../certs"
HOSTNAME="${CADDY_HOSTNAME:-localhost}"
DAYS=365

echo "[dev-certs] Generating self-signed certificate for: ${HOSTNAME}"
echo "[dev-certs] Output directory: ${CERTS_DIR}"
echo "[dev-certs] Valid for: ${DAYS} days"

mkdir -p "${CERTS_DIR}"

# MSYS_NO_PATHCONV=1 prevents Git Bash on Windows from converting the
# -subj string (e.g. /CN=localhost) into a Windows path (C:/Program Files/Git/CN=...).
# Harmless on Linux/macOS where the variable is simply ignored.
MSYS_NO_PATHCONV=1 openssl req -x509 \
    -newkey rsa:4096 \
    -keyout "${CERTS_DIR}/server.key" \
    -out    "${CERTS_DIR}/server.crt" \
    -days   "${DAYS}" \
    -nodes \
    -subj   "/CN=${HOSTNAME}/O=BESS-UPF Dev/C=XX" \
    -addext "subjectAltName=DNS:${HOSTNAME},DNS:localhost,IP:127.0.0.1"

chmod 600 "${CERTS_DIR}/server.key"
chmod 644 "${CERTS_DIR}/server.crt"

echo "[dev-certs] Done. Files written:"
echo "  ${CERTS_DIR}/server.crt"
echo "  ${CERTS_DIR}/server.key"
echo ""
echo "[dev-certs] To trust this cert locally (macOS):"
echo "  sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain ${CERTS_DIR}/server.crt"
echo "[dev-certs] To trust this cert locally (Linux/NSS):"
echo "  certutil -d sql:\$HOME/.pki/nssdb -A -t C -n bess-upf-dev -i ${CERTS_DIR}/server.crt"
