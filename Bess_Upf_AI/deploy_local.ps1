$ErrorActionPreference = "Stop"

Write-Host "Building Docker images..."
docker build -t ghcr.io/that-geekyguy/bess-upf-pipeline:local-dev ./pipeline
docker build -t ghcr.io/that-geekyguy/bess-upf-analysis:local-dev ./analysis
docker build -t ghcr.io/that-geekyguy/bess-upf-serve:local-dev ./serve
docker build -t ghcr.io/that-geekyguy/bess-upf-frontend:local-dev ./frontend

Write-Host "Loading images into Kind cluster..."
kind load docker-image ghcr.io/that-geekyguy/bess-upf-pipeline:local-dev --name bess-upf
kind load docker-image ghcr.io/that-geekyguy/bess-upf-analysis:local-dev --name bess-upf
kind load docker-image ghcr.io/that-geekyguy/bess-upf-serve:local-dev --name bess-upf
kind load docker-image ghcr.io/that-geekyguy/bess-upf-frontend:local-dev --name bess-upf

Write-Host "Restarting Deployments..."
kubectl rollout restart deployment/pipeline -n bess-upf
kubectl rollout restart deployment/analysis -n bess-upf
kubectl rollout restart deployment/frontend -n bess-upf

Write-Host "Restarting Ray Serve pods (KubeRay will recreate them)..."
kubectl delete pod -l ray.io/node-type=worker -n bess-upf
kubectl delete pod -l ray.io/node-type=head -n bess-upf

Write-Host "Deployment complete! Run 'kubectl get pods -n bess-upf -w' to watch them come back up."
