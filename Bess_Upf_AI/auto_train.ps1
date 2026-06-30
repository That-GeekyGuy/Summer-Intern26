$ErrorActionPreference = "Continue"

Write-Host "Waiting for MOMENT training to finish..."
$momentExitCode = docker wait moment_gpu_train
Write-Host "MOMENT exited with code: $momentExitCode"
docker logs moment_gpu_train 2>&1 | Out-File moment_train.log

Write-Host "Cleaning up old Chronos container if exists..."
docker rm -f chronos_gpu_train 2>$null

Write-Host "Starting Chronos GPU training..."
$chronosId = docker run -d --name chronos_gpu_train --gpus all --runtime=nvidia -e NVIDIA_VISIBLE_DEVICES=all -e CUDA_VISIBLE_DEVICES=0 -e PYTORCH_CUDA_ALLOC_CONF=max_split_size_mb:512 -e HF_HOME=/hf-cache -v "$PWD/models:/models" -v "$PWD/tools:/app/tools" -v "bess_upf_ai_hf-cache:/hf-cache" bess_upf_ai-chronos-sidecar python3 tools/train/train_chronos.py --data-dir tools/train/data --models-dir /models
Write-Host "Chronos container started: $chronosId"

Write-Host "Waiting for Chronos to finish..."
$chronosExitCode = docker wait chronos_gpu_train
Write-Host "Chronos exited with code: $chronosExitCode"
docker logs chronos_gpu_train 2>&1 | Out-File chronos_train.log

Write-Host "Cleaning up ad-hoc containers..."
docker rm moment_gpu_train chronos_gpu_train

Write-Host "Restarting docker compose stack (which will now use the GPU sidecars)..."
docker compose up -d

Write-Host "Checking health of AI sidecars..."
Start-Sleep -Seconds 10
docker ps | Select-String "sidecar"

Write-Host "Training pipeline automated successfully!"
