# Detect GPU/CPU and start the appropriate Docker Compose stack.
# Windows PowerShell equivalent of scripts/start.sh
#
# Usage: pwsh scripts/start.ps1 [extra docker compose args...]
# Examples:
#   pwsh scripts/start.ps1 --build -d
#   pwsh scripts/start.ps1 -d

param([Parameter(ValueFromRemainingArguments=$true)][string[]]$ComposeArgs)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$COMPOSE_BASE = @('-f', 'docker-compose.yml')
$DEV_COMPOSE  = @('-f', 'dev/docker-compose.yml')
$ComposeFiles = $COMPOSE_BASE
$Mode = 'cpu'

function Info  { Write-Host "[start.ps1] $args" -ForegroundColor Green }
function Warn  { Write-Host "[start.ps1] $args" -ForegroundColor Yellow }

# ── 1. Detect NVIDIA GPU + Docker nvidia runtime ──────────────────────────────
function Test-GpuAvailable {
    if (-not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
        Warn "nvidia-smi not found."
        return $false
    }
    nvidia-smi 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Warn "nvidia-smi failed -- driver missing?"
        return $false
    }
    $dockerInfo = docker info 2>&1
    if ($dockerInfo -notmatch 'nvidia') {
        Warn "NVIDIA GPU found but Docker runtime not configured."
        Warn "Fix: nvidia-ctk runtime configure --runtime=docker (then restart Docker Desktop)"
        return $false
    }
    return $true
}

# ── 2. Read VRAM and select model ─────────────────────────────────────────────
function Set-GpuModel {
    $vramMb = (nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>$null |
        Select-Object -First 1).Trim()
    $vramGb = [int]([int]$vramMb / 1024)
    Info "Detected GPU VRAM: ${vramGb} GB"

    if ($vramGb -ge 24) {
        Info ">= 24 GB -> Qwen/Qwen3-14B FP16 (max_len 8192)"
        $env:VLLM_MODEL         = 'Qwen/Qwen2.5-14B-Instruct'
        $env:VLLM_MAX_MODEL_LEN = '8192'
        $env:VLLM_GPU_MEM_UTIL  = '0.90'
    } elseif ($vramGb -ge 13) {
        Info "13-23 GB -> Qwen/Qwen3-8B FP16 (max_len 8192)"
        $env:VLLM_MODEL         = 'Qwen/Qwen2.5-7B-Instruct'
        $env:VLLM_MAX_MODEL_LEN = '8192'
        $env:VLLM_GPU_MEM_UTIL  = '0.90'
    } elseif ($vramGb -ge 8) {
        Info "8-12 GB -> Qwen/Qwen3-8B INT4 (max_len 3200)"
        $env:VLLM_MODEL         = 'Qwen/Qwen2.5-7B-Instruct'
        $env:VLLM_MAX_MODEL_LEN = '3200'
        $env:VLLM_GPU_MEM_UTIL  = '0.90'
    } else {
        Warn "VRAM ${vramGb} GB < 8 GB -- insufficient for vLLM. Falling back to CPU/Ollama."
        return $false
    }
    return $true
}

# ── 3. Pick stack ──────────────────────────────────────────────────────────────
if ((Test-GpuAvailable) -and (Set-GpuModel)) {
    $Mode = 'gpu'
    $ComposeFiles = $COMPOSE_BASE + @('-f', 'docker-compose.gpu.yml')
    Info "Mode: GPU  ->  vLLM  model=$($env:VLLM_MODEL)"
} else {
    $Mode = 'cpu'
    $ComposeFiles = $COMPOSE_BASE + @('-f', 'docker-compose.cpu.yml')
    if (-not $env:OLLAMA_MODEL) { $env:OLLAMA_MODEL = 'qwen2.5:3b' }

    # Windows RAM via WMI
    try {
        $ramGb = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
    } catch {
        $ramGb = 0
    }
    Info "Mode: CPU  ->  Ollama  model=$($env:OLLAMA_MODEL)  RAM=${ramGb}GB"
    if ($ramGb -gt 0 -and $ramGb -lt 8) {
        Warn "Low RAM (${ramGb} GB). Consider: OLLAMA_MODEL=qwen2.5:1.5b"
    }
}

# ── 4. Dev services (prometheus + upf-sim + grafana) ──────────────────────────
if ([Environment]::UserInteractive) {
    $devChoice = Read-Host '[start.ps1] Start dev services (prometheus, upf-sim, grafana)? [y/N]'
    if ($devChoice -eq 'y') {
        $ComposeFiles += $DEV_COMPOSE
        Info "Dev services: enabled"
    } else {
        Info "Dev services: skipped (external Prometheus mode)"
    }
}

# ── 5. Start ───────────────────────────────────────────────────────────────────
$cmd = $ComposeFiles + @('up') + $ComposeArgs
Info "docker compose $($cmd -join ' ')"
& docker compose @cmd

# ── 6. Pull Ollama model (CPU mode only) ───────────────────────────────────────
if ($Mode -eq 'cpu') {
    $ollamaModel = if ($env:OLLAMA_MODEL) { $env:OLLAMA_MODEL } else { 'qwen2.5:3b' }
    Info "Waiting for Ollama to be ready..."
    $retries = 24
    while ($retries-- -gt 0) {
        & docker compose @ComposeFiles exec vllm ollama list 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { break }
        Start-Sleep 5
    }
    Info "Pulling $ollamaModel (first run downloads ~2 GB)..."
    & docker compose @ComposeFiles exec vllm ollama pull $ollamaModel
    Info "Ollama model ready."
}

$hostname = if ($env:CADDY_HOSTNAME) { $env:CADDY_HOSTNAME } else { 'localhost' }
Info "Stack is up. Open https://$hostname"
