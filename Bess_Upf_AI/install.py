#!/usr/bin/env python3
"""One-shot installer for the BESS-UPF v2 stack (kind + Helm).

Brings up a local kind cluster (or reuses an existing one), builds and
loads every service image, and helm-installs the full chart. Safe to
re-run — every step is idempotent.

Usage:
    python install.py                    # full install; upf-sim (generator) OFF by default
    python install.py --generator        # include upf-sim; use for local dev without a real UPF
    python install.py --tag phase4a      # image tag to build/deploy (default: local-dev)
    python install.py --skip-build       # redeploy without rebuilding images
    python install.py --services=analysis,frontend
                                          # rebuild/reload only the named services (comma-separated)
                                          # instead of all of them, then roll their pods to pick up
                                          # the fresh image (kind load alone doesn't restart anything
                                          # already running under the same tag).

Requires Python 3.7+ and, already installed on PATH: docker, kind, kubectl, helm.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
CLUSTER_NAME = "bess-upf"
NAMESPACE = "bess-upf"
REGISTRY = "ghcr.io/that-geekyguy"
SERVICES = ["pipeline", "analysis", "serve", "frontend", "mitigation", "chronos", "tools"]
GENERATOR_SERVICE = "upf-sim"

# Services backed by a plain apps/v1 Deployment — `kubectl rollout restart
# deployment/<name>` applies directly. `serve` (a RayCluster, not a
# Deployment) and `tools` (only ever used inside Job pod specs, never a
# running Deployment) are handled separately in rollout_restart_services.
DEPLOYMENT_SERVICES = {"pipeline", "analysis", "frontend", "mitigation", "chronos", GENERATOR_SERVICE}

# Fixed NodePort the ingress-nginx controller is pinned to (via a --set flag
# in helm_deploy) so kind's extraPortMappings has a stable target. Must stay
# in sync with k8s/kind-config.yaml's containerPort and render_kind_config().
INGRESS_NODE_PORT = 30080

# .env keys that map straight onto a Helm value path. Only keys present (and
# non-empty) in .env produce a --set flag, so anything left unset falls back
# to the chart's own values.yaml defaults.
ENV_TO_HELM = {
    "INGRESS_HOST": "global.ingressHost",
    "ANALYSIS_AUTH_USER": "analysis.authUser",
    "ANALYSIS_AUTH_PASSWORD": "analysis.authPassword",
    "VLLM_URL": "analysis.vllmUrl",
    "SIM_URL": "analysis.simUrl",
    "RAY_SERVE_URL": "pipeline.rayServeUrl",
    "CHRONOS_URL": "pipeline.chronosUrl",
    "UPF_SIM_METRICS_URL": "pipeline.metricsUrl",
    "SCRAPE_INTERVAL_SECS": "pipeline.scrapeIntervalSecs",
    "FORECAST_CHECK_INTERVAL_SECS": "pipeline.forecastCheckIntervalSecs",
    "CHRONOS_STEP_SECONDS": "chronos.stepSeconds",
    "MITIGATION_API_PORT": "mitigation.apiPort",
    "CLICKHOUSE_PASSWORD": "clickhouse.password",
}
# .env keys that fan out to more than one Helm value path.
ENV_TO_HELM_MULTI = {
    "KAFKA_BROKER": ["pipeline.kafkaBroker", "mitigation.kafkaBroker"],
}


def load_dotenv():
    """Read .env (if present) into a dict and merge it into os.environ.

    Comment lines (#) and blanks are skipped; values may be quoted. Existing
    os.environ entries win (so a real env var still overrides .env), matching
    common dotenv convention.
    """
    path = os.path.join(ROOT, ".env")
    values = {}
    if not os.path.isfile(path):
        return values
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            values[key] = val
            os.environ.setdefault(key, val)
    return values


def resolve_local_port(env_values):
    raw = env_values.get("LOCAL_PORT", "8080")
    try:
        return int(raw)
    except ValueError:
        sys.exit("ERROR: LOCAL_PORT in .env must be an integer, got {!r}".format(raw))


def should_skip_build(skip_build_flag, cluster_recreated):
    """A freshly (re)created cluster has no images loaded yet, so --skip-build
    can never actually be honored in that case — it only applies when the
    existing cluster was reused unchanged.
    """
    return skip_build_flag and not cluster_recreated


# MITIGATION_API_PORT feeds a containerPort (must stay a YAML int); everything
# else is treated as a plain string via --set-string to dodge --set's YAML
# type coercion (a password of "123" or a "true"-looking value staying literal).
_NUMERIC_ENV_KEYS = {"MITIGATION_API_PORT"}


def helm_set_flags_from_env(env_values):
    flags = []
    for key, path in ENV_TO_HELM.items():
        val = env_values.get(key)
        if val:
            flag = "--set" if key in _NUMERIC_ENV_KEYS else "--set-string"
            flags += [flag, "{}={}".format(path, val)]
    for key, paths in ENV_TO_HELM_MULTI.items():
        val = env_values.get(key)
        if val:
            for path in paths:
                flags += ["--set-string", "{}={}".format(path, val)]
    return flags


def helm_set_flags_from_vllm(vllm_config):
    flags = ["--set", "analysis.vllm.mock.enabled=" + ("true" if vllm_config["mock"] else "false")]
    if not vllm_config["mock"]:
        flags += [
            "--set-string", "analysis.vllm.model=" + vllm_config["model"],
            "--set", "analysis.vllm.maxModelLen=" + str(vllm_config["max_model_len"]),
            "--set-string", "analysis.vllm.gpuMemoryUtilization=" + str(vllm_config["gpu_mem_util"]),
        ]
    return flags


def ensure_hf_token_secret(env_values):
    token = env_values.get("HF_TOKEN")
    if not token:
        return
    yaml_text = subprocess.run(
        ["kubectl", "create", "secret", "generic", "hf-token",
         "--namespace", NAMESPACE, "--from-literal=token=" + token,
         "--dry-run=client", "-o", "yaml"],
        capture_output=True, text=True, check=True,
    ).stdout
    kubectl_apply_yaml(yaml_text)


def render_kind_config(local_port):
    """Render the kind cluster config YAML, with extraPortMappings wiring
    the given host port to the pinned ingress NodePort on the control-plane
    node. Mirrors k8s/kind-config.yaml's structure (3 workers, each with the
    ./models mount) plus the port mapping.
    """
    return """kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
    extraPortMappings:
      - containerPort: {node_port}
        hostPort: {local_port}
        protocol: TCP
  - role: worker
    extraMounts:
      - hostPath: ./models
        containerPath: /models
  - role: worker
    extraMounts:
      - hostPath: ./models
        containerPath: /models
  - role: worker
    extraMounts:
      - hostPath: ./models
        containerPath: /models
""".format(node_port=INGRESS_NODE_PORT, local_port=local_port)


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


def ensure_cluster(local_port):
    """Ensure the kind cluster exists with local_port mapped to the pinned
    ingress NodePort. Returns True if the cluster was (re)created (so it has
    no images loaded yet), False if an existing, correctly-mapped cluster
    was reused unchanged.
    """
    existing = subprocess.run(["kind", "get", "clusters"], capture_output=True, text=True).stdout.split()
    if CLUSTER_NAME in existing:
        current_port = get_current_port_mapping()
        if current_port == local_port:
            print("kind cluster '{}' already exists, reusing (port {} already mapped).".format(
                CLUSTER_NAME, local_port))
            return False
        print("kind cluster '{}' exists but maps host port {} (want {}); recreating.".format(
            CLUSTER_NAME, current_port, local_port))
        run(["kind", "delete", "cluster", "--name", CLUSTER_NAME])
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(render_kind_config(local_port))
        config_path = f.name
    try:
        run(["kind", "create", "cluster", "--name", CLUSTER_NAME, "--config", config_path])
    finally:
        os.unlink(config_path)
    return True


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


def parse_nvidia_smi_output(text):
    """Parse `nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits`
    output into the max VRAM (MiB) across all reported GPUs, or None if
    there's no usable number in the output.
    """
    values = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            values.append(int(line))
        except ValueError:
            continue
    return max(values) if values else None


def detect_vram_mib():
    """Best-effort local VRAM detection. Returns None (→ mock) whenever
    nvidia-smi is missing, errors, or produces nothing parseable — this
    covers no-GPU boxes, non-NVIDIA GPUs, and CI/test runners equally.
    """
    if shutil.which("nvidia-smi") is None:
        return None
    result = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return parse_nvidia_smi_output(result.stdout)


# (min_vram_mib, model, max_model_len, gpu_memory_utilization), ascending.
# Below the first threshold (or no GPU detected) → mock.
VLLM_TIERS = [
    (8192, "Qwen/Qwen2.5-3B-Instruct-AWQ", 4096, "0.85"),
    (16384, "Qwen/Qwen2.5-7B-Instruct-AWQ", 2048, "0.8"),
    (24576, "Qwen/Qwen2.5-14B-Instruct-AWQ", 4096, "0.85"),
]


def resolve_vllm_tier(vram_mib):
    """Return (model, max_model_len, gpu_mem_util) for the highest tier
    vram_mib qualifies for, or None if vram_mib is None or below the
    lowest tier's threshold (caller should use mock).
    """
    if vram_mib is None:
        return None
    chosen = None
    for min_mib, model, max_len, util in VLLM_TIERS:
        if vram_mib >= min_mib:
            chosen = (model, max_len, util)
    return chosen


def resolve_vllm_config(env_values, vram_mib):
    """Resolve final vLLM settings from .env's VLLM_MODE/VLLM_MODEL_OVERRIDE
    and detected VRAM. Returns a dict with keys: mock (bool), model
    (str|None), max_model_len (int|None), gpu_mem_util (str|None).
    """
    mode = (env_values.get("VLLM_MODE") or "auto").strip().lower()
    override = env_values.get("VLLM_MODEL_OVERRIDE") or None

    if mode == "mock":
        return {"mock": True, "model": None, "max_model_len": None, "gpu_mem_util": None}

    if mode == "real":
        tier = resolve_vllm_tier(vram_mib) or resolve_vllm_tier(16384)
        model, max_len, util = tier
        return {"mock": False, "model": override or model, "max_model_len": max_len, "gpu_mem_util": util}

    # auto
    tier = resolve_vllm_tier(vram_mib)
    if tier is None:
        return {"mock": True, "model": None, "max_model_len": None, "gpu_mem_util": None}
    model, max_len, util = tier
    return {"mock": False, "model": override or model, "max_model_len": max_len, "gpu_mem_util": util}


def get_worker_nodes():
    out = subprocess.run(
        ["docker", "ps", "--filter", "name=" + CLUSTER_NAME + "-worker", "--format", "{{.Names}}"],
        capture_output=True, text=True, check=True,
    ).stdout
    names = [n for n in out.split() if n]
    return names or [CLUSTER_NAME + "-control-plane"]


def parse_port_binding(bindings_json_text, container_port):
    """Parse `docker inspect -f '{{json .HostConfig.PortBindings}}'` output
    and return the host port bound to container_port/tcp, or None if there's
    no such binding (including malformed/empty input).
    """
    try:
        bindings = json.loads(bindings_json_text)
    except (ValueError, TypeError):
        return None
    if not bindings:
        return None
    entries = bindings.get("{}/tcp".format(container_port))
    if not entries:
        return None
    host_port = entries[0].get("HostPort")
    return int(host_port) if host_port else None


def get_current_port_mapping():
    """Return the host port currently bound to INGRESS_NODE_PORT on the
    control-plane container, or None if the container doesn't exist or has
    no such binding.
    """
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{json .HostConfig.PortBindings}}",
         CLUSTER_NAME + "-control-plane"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return parse_port_binding(result.stdout.strip(), INGRESS_NODE_PORT)


def resolve_build_services(services_arg, include_generator):
    """Resolve --services into the concrete list of services to build/load
    this run. None (flag omitted) means everything (SERVICES, plus
    GENERATOR_SERVICE when --generator is set). Validates names against the
    known set and rejects requesting upf-sim without --generator (it won't
    be deployed, so building it would be pointless).
    """
    all_services = list(SERVICES)
    if include_generator:
        all_services.append(GENERATOR_SERVICE)
    if services_arg is None:
        return all_services

    requested = [s.strip() for s in services_arg.split(",") if s.strip()]
    valid = set(SERVICES) | {GENERATOR_SERVICE}
    unknown = [s for s in requested if s not in valid]
    if unknown:
        sys.exit("ERROR: unknown service(s) in --services: {}. Valid: {}".format(
            ", ".join(unknown), ", ".join(sorted(valid))))
    if GENERATOR_SERVICE in requested and not include_generator:
        sys.exit("ERROR: --services includes '{}' but --generator wasn't passed; "
                  "it won't be deployed, so building it is pointless. Add --generator too.".format(
                      GENERATOR_SERVICE))
    return requested


def build_and_load(tag, services):
    nodes = ",".join(get_worker_nodes())
    for svc in services:
        image = "{}/bess-upf-{}:{}".format(REGISTRY, svc, tag)
        run(["docker", "build", "-t", image, os.path.join(ROOT, svc)])
        run(["kind", "load", "docker-image", image, "--name", CLUSTER_NAME, "--nodes", nodes])


def rollout_restart_services(services):
    """Force already-running pods to pick up freshly (re)built images.
    `kind load` only refreshes the node's local image cache under the same
    tag — Kubernetes has no signal that the content changed, so an existing
    pod keeps running its old container until something restarts it.
    """
    for svc in services:
        if svc in DEPLOYMENT_SERVICES:
            run(["kubectl", "rollout", "restart", "deployment/" + svc, "-n", NAMESPACE])
        elif svc == "serve":
            # RayCluster, not a Deployment — kuberay-operator recreates
            # deleted pods from the current (freshly-loaded) image.
            run(["kubectl", "delete", "pod", "-n", NAMESPACE, "-l", "ray.io/cluster=bess-upf-serve"])
        elif svc == "tools":
            print("tools: no running Deployment to restart — picked up automatically "
                  "by the next Job that uses it (e.g. the benchmark-report hook).")


def helm_deploy(tag, include_generator, timeout_min, env_values, vllm_config):
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
        "--set", "ingress-nginx.controller.service.nodePorts.http=" + str(INGRESS_NODE_PORT),
    ] + helm_set_flags_from_env(env_values) + helm_set_flags_from_vllm(vllm_config) + [
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
    parser.add_argument("--generator", action="store_true",
                         help="Deploy upf-sim (the synthetic UPF traffic generator). "
                              "Off by default — most environments are fed by a real UPF.")
    parser.add_argument("--tag", default="local-dev", help="Image tag to build and deploy (default: local-dev)")
    parser.add_argument("--skip-build", action="store_true",
                         help="Skip docker build/kind load (redeploy existing images)")
    parser.add_argument("--services", default=None,
                         help="Comma-separated services to rebuild/reload (default: all). "
                              "E.g. --services=analysis,frontend. Rebuilt services are rolled "
                              "afterwards so their pods actually pick up the new image.")
    parser.add_argument("--timeout", type=int, default=10, help="Minutes to wait per stage (default: 10)")
    args = parser.parse_args()

    include_generator = args.generator
    env_values = load_dotenv()
    if env_values:
        print("Loaded {} value(s) from .env".format(len(env_values)))

    print("== 1/6 Checking prerequisites ==")
    check_prereqs()

    print("== 2/6 Ensuring kind cluster ==")
    local_port = resolve_local_port(env_values)
    cluster_recreated = ensure_cluster(local_port)

    print("== 3/6 Namespace + kuberay-operator + pull secret ==")
    ensure_namespace()
    install_kuberay_operator()
    ensure_ghcr_secret()
    ensure_hf_token_secret(env_values)

    skip_build = should_skip_build(args.skip_build, cluster_recreated)
    if cluster_recreated and args.skip_build:
        print("Cluster was just (re)created — it has no images loaded yet, ignoring --skip-build for this run.")

    build_services = []
    if not skip_build:
        build_services = resolve_build_services(args.services, include_generator)
        print("== 4/6 Building and loading images ({}) ==".format(", ".join(build_services)))
        build_and_load(args.tag, build_services)
    else:
        if args.services:
            print("== 4/6 Skipping build (--skip-build) — --services={} ignored ==".format(args.services))
        else:
            print("== 4/6 Skipping build (--skip-build) ==")

    vram_mib = detect_vram_mib()
    vllm_config = resolve_vllm_config(env_values, vram_mib)
    if vllm_config["mock"]:
        print("vLLM: mock mode (detected VRAM: {})".format(vram_mib if vram_mib is not None else "none/undetected"))
    else:
        print("vLLM: real mode, model={} (detected VRAM: {} MiB)".format(vllm_config["model"], vram_mib))

    print("== 5/6 Helm deploy ==")
    helm_deploy(args.tag, include_generator, args.timeout, env_values, vllm_config)

    if build_services and not cluster_recreated:
        print("Rolling {} to pick up the freshly built image(s)...".format(", ".join(build_services)))
        rollout_restart_services(build_services)

    print("== 6/6 Waiting for stack ==")
    if not wait_for_stack(args.timeout * 60):
        sys.exit(1)

    print("\nBESS-UPF v2 is up. `kubectl get pods -n {} -w` to watch.".format(NAMESPACE))


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        sys.exit("\nFAILED: {} (exit {})".format(" ".join(exc.cmd), exc.returncode))
