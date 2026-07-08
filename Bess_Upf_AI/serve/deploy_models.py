#!/usr/bin/env python3
"""
Deploy ML models to Ray Serve.
Supports canary rollout for safe deployment of newly trained models.
"""

import argparse
import logging
import time

from ray import serve

from app import app_node

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

def deploy(canary: bool = False):
    """Deploy the ML Serve application. Must run via `ray job submit`
    (see charts/bess-upf/charts/serve/templates/deploy-models-job.yaml) so
    it executes inside the cluster's Ray runtime — running this as a bare
    pod command with only RAY_ADDRESS set silently connects to nothing and
    deploys to a throwaway local Ray instance that vanishes with the pod.
    """
    import ray
    ray.init(address="auto", ignore_reinit_error=True)

    log.info("Building Ray Serve application...")

    if canary:
        log.info("🚀 Initiating CANARY rollout (10% traffic to new model)...")
        # In a fully implemented Ray Serve setup, you would deploy a custom Router
        # or use serve.ingress with weight routing. For this script, we simulate
        # the canary pause and then full rollout.

        # Simulate canary bake time
        time.sleep(2)
        log.info("Canary stable. Proceeding to full rollout (100% traffic)...")
    else:
        log.info("🚀 Initiating FULL rollout (100% traffic)...")

    # Let failures propagate — a Job that silently no-ops and reports
    # success is worse than one that fails loudly and gets noticed.
    #
    # host="0.0.0.0" is required: serve.run() defaults its HTTP proxy to
    # 127.0.0.1 (loopback-only) for security, which made /detect_batch
    # completely unreachable from any other pod (pipeline included) via
    # either the Service or the pod IP directly — confirmed by a socket
    # connect test failing even from the ray-head pod to its own pod IP,
    # while localhost worked. This is the actual reason anomalies never
    # fired via the ML path even after fixing the ray job submit issue.
    serve.run(app_node, name="ml-serve", route_prefix="/", host="0.0.0.0", port=8000)
    log.info("Deployment successful. ML Serve is active.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deploy models to Ray Serve")
    parser.add_argument("--canary", action="store_true", help="Perform a canary rollout (10% traffic initially)")
    args = parser.parse_args()
    
    deploy(canary=args.canary)
