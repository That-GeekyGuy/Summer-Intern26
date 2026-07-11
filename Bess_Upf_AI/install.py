#!/usr/bin/env python3
"""One-shot installer for the BESS-UPF v2 stack (kind + Helm).

Brings up a local kind cluster (or reuses an existing one), builds and
loads every service image, and helm-installs the full chart. Safe to
re-run — every step is idempotent.

Usage:
    python install.py                    # full install, generator (upf-sim) included
    python install.py --no-generator     # skip upf-sim; use on a server fed by a real UPF
    python install.py --tag phase4a      # image tag to build/deploy (default: local-dev)
    python install.py --skip-build       # redeploy without rebuilding images

Requires Python 3.7+ and, already installed on PATH: docker, kind, kubectl, helm.
"""
import argparse
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
CLUSTER_NAME = "bess-upf"
NAMESPACE = "bess-upf"
REGISTRY = "ghcr.io/that-geekyguy"
SERVICES = ["pipeline", "analysis", "serve", "frontend", "mitigation", "chronos", "tools"]
GENERATOR_SERVICE = "upf-sim"


def run(cmd, **kw):
    print("$ " + " ".join(cmd))
    subprocess.run(cmd, check=True, **kw)


def require(binary, hint):
    if shutil.which(binary) is None:
        sys.exit("ERROR: '{}' not found on PATH. {}".format(binary, hint))


def check_prereqs():
    require("docker", "Install Docker: https://docs.docker.com/get-docker/")
    require("kind", "Install kind: https://kind.sigs.k8s.io/docs/user/quick-start/#installation")
    require("kubectl", "Install kubectl: https://kubernetes.io/docs/tasks/tools/")
    require("helm", "Install helm: https://helm.sh/docs/intro/install/")
    if subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
        sys.exit("ERROR: Docker daemon not reachable. Start Docker and retry.")


def ensure_cluster():
    existing = subprocess.run(["kind", "get", "clusters"], capture_output=True, text=True).stdout.split()
    if CLUSTER_NAME in existing:
        print("kind cluster '{}' already exists, reusing.".format(CLUSTER_NAME))
        return
    run(["kind", "create", "cluster", "--name", CLUSTER_NAME,
         "--config", os.path.join(ROOT, "k8s", "kind-config.yaml")])


def kubectl_apply_yaml(yaml_text):
    subprocess.run(["kubectl", "apply", "-f", "-"], input=yaml_text, text=True, check=True)


def ensure_namespace():
    yaml_text = subprocess.run(
        ["kubectl", "create", "namespace", NAMESPACE, "--dry-run=client", "-o", "yaml"],
        capture_output=True, text=True, check=True,
    ).stdout
    kubectl_apply_yaml(yaml_text)


def install_kuberay_operator():
    run(["helm", "repo", "add", "kuberay", "https://ray-project.github.io/kuberay-helm/"])
    run(["helm", "repo", "update"])
    run(["helm", "upgrade", "--install", "kuberay-operator", "kuberay/kuberay-operator",
         "--namespace", NAMESPACE, "--version", "1.1.0", "--wait", "--timeout", "3m"])


def ensure_ghcr_secret():
    # Local/kind images are built + loaded directly (no real registry pull needed),
    # so a placeholder is fine unless real creds are supplied for a genuine registry pull.
    user = os.environ.get("GHCR_USERNAME", "local-dev")
    token = os.environ.get("GHCR_TOKEN", "unused")
    yaml_text = subprocess.run(
        ["kubectl", "create", "secret", "docker-registry", "ghcr-pull-secret",
         "--namespace", NAMESPACE, "--docker-server=ghcr.io",
         "--docker-username=" + user, "--docker-password=" + token,
         "--dry-run=client", "-o", "yaml"],
        capture_output=True, text=True, check=True,
    ).stdout
    kubectl_apply_yaml(yaml_text)


def get_worker_nodes():
    out = subprocess.run(
        ["docker", "ps", "--filter", "name=" + CLUSTER_NAME + "-worker", "--format", "{{.Names}}"],
        capture_output=True, text=True, check=True,
    ).stdout
    names = [n for n in out.split() if n]
    return names or [CLUSTER_NAME + "-control-plane"]


def build_and_load(tag, include_generator):
    services = list(SERVICES)
    if include_generator:
        services.append(GENERATOR_SERVICE)
    nodes = ",".join(get_worker_nodes())
    for svc in services:
        image = "{}/bess-upf-{}:{}".format(REGISTRY, svc, tag)
        run(["docker", "build", "-t", image, os.path.join(ROOT, svc)])
        run(["kind", "load", "docker-image", image, "--name", CLUSTER_NAME, "--nodes", nodes])


def helm_deploy(tag, include_generator, timeout_min):
    chart_dir = os.path.join(ROOT, "charts", "bess-upf")
    run(["helm", "dependency", "update"], cwd=chart_dir)
    run([
        "helm", "upgrade", "--install", "bess-upf", chart_dir,
        "-n", NAMESPACE, "--create-namespace",
        "--set", "global.tag=" + tag,
        "--set", "pipeline.tag=" + tag,
        "--set", "analysis.tag=" + tag,
        "--set", "frontend.tag=" + tag,
        "--set", "serve.tag=" + tag,
        "--set", "mitigation.tag=" + tag,
        "--set", "chronos.tag=" + tag,
        "--set", "upf-sim.tag=" + tag,
        "--set", "upf-sim.enabled=" + ("true" if include_generator else "false"),
        "--force",
        "--timeout", "{}m".format(timeout_min),
    ])


def _pod_ok(line):
    parts = line.split()
    ready, status = parts[1], parts[2]
    if status == "Completed":
        return True
    if status != "Running":
        return False
    have, want = ready.split("/")
    return have == want


def wait_for_stack(timeout_s):
    print("Waiting for all pods to become Ready/Completed...")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        out = subprocess.run(
            ["kubectl", "get", "pods", "-n", NAMESPACE, "--no-headers"],
            capture_output=True, text=True,
        ).stdout.strip().splitlines()
        if out:
            healthy = sum(1 for l in out if _pod_ok(l))
            print("  {}/{} pods healthy".format(healthy, len(out)), end="\r")
            if healthy == len(out):
                print()
                return True
        time.sleep(5)
    print("\nTimed out waiting for pods. Current status:")
    subprocess.run(["kubectl", "get", "pods", "-n", NAMESPACE])
    return False


def main():
    parser = argparse.ArgumentParser(description="One-shot BESS-UPF v2 installer")
    parser.add_argument("--no-generator", action="store_true",
                         help="Skip upf-sim (the synthetic UPF traffic generator). "
                              "Use on a server fed by a real UPF instead of the simulator.")
    parser.add_argument("--tag", default="local-dev", help="Image tag to build and deploy (default: local-dev)")
    parser.add_argument("--skip-build", action="store_true",
                         help="Skip docker build/kind load (redeploy existing images)")
    parser.add_argument("--timeout", type=int, default=10, help="Minutes to wait per stage (default: 10)")
    args = parser.parse_args()

    include_generator = not args.no_generator

    print("== 1/6 Checking prerequisites ==")
    check_prereqs()

    print("== 2/6 Ensuring kind cluster ==")
    ensure_cluster()

    print("== 3/6 Namespace + kuberay-operator + pull secret ==")
    ensure_namespace()
    install_kuberay_operator()
    ensure_ghcr_secret()

    if not args.skip_build:
        print("== 4/6 Building and loading images ({}generator) ==".format("" if include_generator else "no "))
        build_and_load(args.tag, include_generator)
    else:
        print("== 4/6 Skipping build (--skip-build) ==")

    print("== 5/6 Helm deploy ==")
    helm_deploy(args.tag, include_generator, args.timeout)

    print("== 6/6 Waiting for stack ==")
    if not wait_for_stack(args.timeout * 60):
        sys.exit(1)

    print("\nBESS-UPF v2 is up. `kubectl get pods -n {} -w` to watch.".format(NAMESPACE))


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        sys.exit("\nFAILED: {} (exit {})".format(" ".join(exc.cmd), exc.returncode))
