#!/usr/bin/env bash
# Detect GPU/CPU and start the appropriate Docker Compose stack.
#
# Usage: bash scripts/start.sh [extra docker compose args...]
# Examples:
#   bash scripts/start.sh --build
#   bash scripts/start.sh --build -d

set -euo pipefail

COMPOSE_BASE="-f docker-compose.yml"
DEV_COMPOSE="-f dev/docker-compose.yml"
COMPOSE_FILES="$COMPOSE_BASE"
MODE="cpu"

# ── Colour output ──────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info() { echo -e "${GREEN}[start.sh]${NC} $*"; }
warn() { echo -e "${YELLOW}[start.sh]${NC} $*"; }

# ── 1. Detect NVIDIA GPU + Docker runtime ─────────────────────────────────────
gpu_available() {
    command -v nvidia-smi &>/dev/null || { warn "nvidia-smi not found."; return 1; }
    nvidia-smi &>/dev/null            || { warn "nvidia-smi failed — driver missing?"; return 1; }
    docker info 2>/dev/null | grep -q "nvidia" || {
        warn "NVIDIA GPU found but Docker runtime not configured."
        warn "Fix: sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker"
        return 1
    }
    return 0
}

# ── 2. Read VRAM and select model ─────────────────────────────────────────────
configure_for_gpu() {
    local vram_mb vram_gb
    vram_mb=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')
    vram_gb=$(( vram_mb / 1024 ))
    info "Detected GPU VRAM: ${vram_gb} GB"

    if (( vram_gb >= 24 )); then
        info "≥ 24 GB → Qwen/Qwen3-14B FP16 (max_len 8192)"
        export VLLM_MODEL="Qwen/Qwen3-14B"
        export VLLM_MAX_MODEL_LEN="8192"
        export VLLM_GPU_MEM_UTIL="0.90"
    elif (( vram_gb >= 13 )); then
        info "13–23 GB → Qwen/Qwen3-8B FP16 (max_len 8192)"
        export VLLM_MODEL="Qwen/Qwen3-8B"
        export VLLM_MAX_MODEL_LEN="8192"
        export VLLM_GPU_MEM_UTIL="0.90"
    elif (( vram_gb >= 8 )); then
        info "8–12 GB → Qwen/Qwen3-8B INT4 (max_len 3200)"
        export VLLM_MODEL="Qwen/Qwen3-8B"
        export VLLM_MAX_MODEL_LEN="3200"
        export VLLM_GPU_MEM_UTIL="0.90"
    else
        warn "VRAM ${vram_gb} GB < 8 GB — insufficient for vLLM. Falling back to CPU/Ollama."
        return 1
    fi
}

# ── 3. Pick stack ──────────────────────────────────────────────────────────────
if gpu_available && configure_for_gpu; then
    MODE="gpu"
    COMPOSE_FILES="${COMPOSE_BASE} -f docker-compose.gpu.yml"
    info "Mode: GPU  →  vLLM  model=${VLLM_MODEL}"
else
    MODE="cpu"
    COMPOSE_FILES="${COMPOSE_BASE} -f docker-compose.cpu.yml"
    OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5:3b}"

    # /proc/meminfo is Linux-only; skip RAM warning on Windows/macOS
    if [[ -f /proc/meminfo ]]; then
        total_ram_kb=$(grep MemTotal /proc/meminfo | awk '{print $2}')
    else
        total_ram_kb=0
    fi
    total_ram_gb=$(( total_ram_kb / 1024 / 1024 ))
    info "Mode: CPU  →  Ollama  model=${OLLAMA_MODEL}  RAM=${total_ram_gb}GB"
    if (( total_ram_gb > 0 && total_ram_gb < 8 )); then
        warn "Low RAM (${total_ram_gb} GB). Consider: OLLAMA_MODEL=qwen2.5:1.5b"
    fi
fi

# ── 4. Dev services (prometheus + upf-sim + grafana) ──────────────────────────
if [[ -t 0 ]]; then
    read -r -p "[start.sh] Start dev services (prometheus, upf-sim, grafana)? [y/N] " dev_choice
    if [[ "${dev_choice,,}" == "y" ]]; then
        COMPOSE_FILES="${COMPOSE_FILES} ${DEV_COMPOSE}"
        info "Dev services: enabled"
    else
        info "Dev services: skipped (external Prometheus mode)"
    fi
fi

# ── 5. Start ───────────────────────────────────────────────────────────────────
info "docker compose ${COMPOSE_FILES} up $*"
# shellcheck disable=SC2086
docker compose ${COMPOSE_FILES} up "$@"

# ── 6. Pull Ollama model (CPU mode only) ───────────────────────────────────────
if [[ "$MODE" == "cpu" ]]; then
    OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5:3b}"
    info "Waiting for Ollama to be ready..."
    retries=24
    while (( retries-- > 0 )); do
        # shellcheck disable=SC2086
        if docker compose ${COMPOSE_FILES} exec vllm ollama list &>/dev/null 2>&1; then
            break
        fi
        sleep 5
    done
    info "Pulling ${OLLAMA_MODEL} (first run downloads ~2 GB)..."
    # shellcheck disable=SC2086
    docker compose ${COMPOSE_FILES} exec vllm ollama pull "${OLLAMA_MODEL}"
    info "Ollama model ready."
fi

info "Stack is up. Open https://${CADDY_HOSTNAME:-localhost}"
