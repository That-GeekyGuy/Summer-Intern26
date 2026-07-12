# install.py Portability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `install.py` deploy without the generator by default, reachable at `localhost` with zero manual port-forwarding, and with a vLLM model chosen by detected VRAM (mock as the safe fallback) — all portable to machines with no NVIDIA GPU.

**Architecture:** All new logic lives in `install.py` as small, independently-testable pure functions (parsing, tier resolution, YAML rendering) wrapped by thin `subprocess`-calling functions. No new external Python dependencies — stdlib only, matching `install.py`'s existing zero-dependency style. Two Helm chart files get a narrow, targeted fix/addition each.

**Tech Stack:** Python 3.7+ stdlib (`argparse`, `subprocess`, `json`, `tempfile`, `shutil`), `unittest` for tests, Helm/`kind`/`kubectl`/`docker` CLIs (already required).

## Global Constraints

- No new pip dependencies in `install.py` (spec Non-goals; keep it runnable on a bare Python 3.7+ with only stdlib).
- VRAM tier thresholds are strict MiB values, confirmed: `<8192` mock, `8192–16383` → Qwen2.5-3B-Instruct-AWQ, `16384–24575` → Qwen2.5-7B-Instruct-AWQ, `>=24576` → Qwen2.5-14B-Instruct-AWQ (spec §3).
- Ingress NodePort is pinned to `30080` everywhere it's referenced — this exact number must match between `install.py`'s `INGRESS_NODE_PORT` constant, the generated `kind` config's `containerPort`, and the Helm `--set` flag (spec §2).
- Recreating the `kind` cluster on a `LOCAL_PORT` mismatch is expected/routine (matches existing RUNBOOK.md §6.2 guidance) — not a bug to work around.
- No GPU-scheduling-inside-`kind` work (NVIDIA Container Toolkit / device plugin) — explicitly out of scope (spec Non-goals).

---

### Task 1: Generator opt-in flag + docs

**Files:**
- Modify: `install.py` (docstring at top, and the `argparse` setup + `include_generator` line inside `main()`)
- Modify: `README.md` (Quick Start section, `### Install` code block)

**Interfaces:**
- Produces: `args.generator` (bool, replaces `args.no_generator`) consumed by the rest of `main()` exactly as `include_generator` was before — no other function signature changes.

- [ ] **Step 1: Update the module docstring's usage examples**

In `install.py`, replace:
```python
Usage:
    python install.py                    # full install, generator (upf-sim) included
    python install.py --no-generator     # skip upf-sim; use on a server fed by a real UPF
    python install.py --tag phase4a      # image tag to build/deploy (default: local-dev)
    python install.py --skip-build       # redeploy without rebuilding images
```
with:
```python
Usage:
    python install.py                    # full install; upf-sim (generator) OFF by default
    python install.py --generator        # include upf-sim; use for local dev without a real UPF
    python install.py --tag phase4a      # image tag to build/deploy (default: local-dev)
    python install.py --skip-build       # redeploy without rebuilding images
```

- [ ] **Step 2: Replace the `--no-generator` argparse flag with `--generator`**

In `main()`, replace:
```python
    parser.add_argument("--no-generator", action="store_true",
                         help="Skip upf-sim (the synthetic UPF traffic generator). "
                              "Use on a server fed by a real UPF instead of the simulator.")
```
with:
```python
    parser.add_argument("--generator", action="store_true",
                         help="Deploy upf-sim (the synthetic UPF traffic generator). "
                              "Off by default — most environments are fed by a real UPF.")
```

- [ ] **Step 3: Flip the default in `include_generator`**

Replace:
```python
    include_generator = not args.no_generator
```
with:
```python
    include_generator = args.generator
```

- [ ] **Step 4: Verify the CLI change**

Run: `python3 install.py --help`
Expected: help text shows `--generator` (not `--no-generator`), with the new description.

Run: `python3 -c "
import sys, os
sys.argv = ['install.py']
import argparse
p = argparse.ArgumentParser()
p.add_argument('--generator', action='store_true')
args = p.parse_args([])
assert args.generator is False
args = p.parse_args(['--generator'])
assert args.generator is True
print('OK')
"`
Expected: `OK`

- [ ] **Step 5: Update README Quick Start**

In `README.md`, under `### Install`, replace:
```bash
python install.py                  # full stack, generator (upf-sim) included
python install.py --no-generator   # skip upf-sim — use when a real UPF feeds Kafka
python install.py --skip-build     # redeploy without rebuilding images
```
with:
```bash
python install.py                  # full stack; upf-sim (generator) OFF by default
python install.py --generator      # include upf-sim — use for local dev without a real UPF
python install.py --skip-build     # redeploy without rebuilding images
```

Directly below that code block, add:
```markdown
This brings up (or reuses) a local `kind` cluster, builds and loads every
service image, installs `kuberay-operator`, helm-installs the full chart,
and waits until every pod is actually healthy. Safe to re-run.

The frontend/API are reachable at `http://localhost:8080` immediately after
install completes — no manual `kubectl port-forward` needed. Override the
port via `.env`'s `LOCAL_PORT` (see `.env.example`).
```
(This paragraph replaces the existing "This brings up (or reuses)..." paragraph that already follows the install code block — merge rather than duplicate.)

- [ ] **Step 6: Commit**

```bash
git add install.py README.md
git commit -m "feat(install): generator opt-in via --generator, off by default"
```

---

### Task 2: `render_kind_config` + pinned ingress NodePort constant

**Files:**
- Modify: `install.py` (add `INGRESS_NODE_PORT` constant near the top, add `render_kind_config` function)
- Modify: `k8s/kind-config.yaml` (add `extraPortMappings` block with the default port, for anyone running `kind create cluster --config k8s/kind-config.yaml` manually)
- Create: `tests/test_install.py`

**Interfaces:**
- Produces: `INGRESS_NODE_PORT = 30080` (module constant), `render_kind_config(local_port: int) -> str` (returns full `kind` cluster config YAML text). Both consumed by Task 4.

- [ ] **Step 1: Add the `INGRESS_NODE_PORT` constant**

In `install.py`, right after `GENERATOR_SERVICE = "upf-sim"`, add:
```python
# Fixed NodePort the ingress-nginx controller is pinned to (via a --set flag
# in helm_deploy) so kind's extraPortMappings has a stable target. Must stay
# in sync with k8s/kind-config.yaml's containerPort and render_kind_config().
INGRESS_NODE_PORT = 30080
```

- [ ] **Step 2: Add `render_kind_config`**

In `install.py`, add this function (anywhere before `ensure_cluster`, e.g. right after the `INGRESS_NODE_PORT` constant block or near `get_worker_nodes` — exact position doesn't matter, module-level function ordering is not significant in Python):
```python
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
```

- [ ] **Step 3: Create the test file with a failing test**

Create `tests/test_install.py`:
```python
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import install


class TestRenderKindConfig(unittest.TestCase):
    def test_maps_local_port_to_pinned_node_port(self):
        text = install.render_kind_config(9090)
        self.assertIn("containerPort: {}".format(install.INGRESS_NODE_PORT), text)
        self.assertIn("hostPort: 9090", text)

    def test_preserves_three_worker_mounts(self):
        text = install.render_kind_config(8080)
        self.assertEqual(text.count("role: worker"), 3)
        self.assertEqual(text.count("containerPath: /models"), 3)

    def test_default_port_example(self):
        text = install.render_kind_config(8080)
        self.assertIn("hostPort: 8080", text)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 tests/test_install.py -v`
Expected: all 3 tests in `TestRenderKindConfig` PASS (the function already exists from Step 2, so this confirms correctness, not TDD red/green — `render_kind_config` is simple enough to write and verify in one pass).

- [ ] **Step 5: Update the checked-in `k8s/kind-config.yaml` to match, at the default port**

Replace the full contents of `k8s/kind-config.yaml` with:
```yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
    extraPortMappings:
      - containerPort: 30080
        hostPort: 8080
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
```

- [ ] **Step 6: Verify the checked-in file is byte-identical to what the function renders at the default port**

Run:
```bash
python3 -c "
import sys
sys.path.insert(0, '.')
import install
rendered = install.render_kind_config(8080)
with open('k8s/kind-config.yaml') as f:
    checked_in = f.read()
assert rendered == checked_in, 'mismatch between render_kind_config(8080) and k8s/kind-config.yaml'
print('OK: checked-in file matches render_kind_config(8080)')
"
```
Expected: `OK: checked-in file matches render_kind_config(8080)`

- [ ] **Step 7: Commit**

```bash
git add install.py k8s/kind-config.yaml tests/test_install.py
git commit -m "feat(install): render kind config with pinned ingress port mapping"
```

---

### Task 3: Docker port-binding inspection

**Files:**
- Modify: `install.py` (add `parse_port_binding` and `get_current_port_mapping`)
- Modify: `tests/test_install.py` (add `TestParsePortBinding`)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `parse_port_binding(bindings_json_text: str, container_port: int) -> int | None`, `get_current_port_mapping() -> int | None`. Both consumed by Task 4's `ensure_cluster`.

- [ ] **Step 1: Add the failing tests**

In `tests/test_install.py`, add (before the `if __name__ == "__main__":` line):
```python
class TestParsePortBinding(unittest.TestCase):
    def test_extracts_host_port_for_matching_container_port(self):
        raw = '{"30080/tcp":[{"HostIp":"","HostPort":"8080"}],"6443/tcp":[{"HostIp":"127.0.0.1","HostPort":"42617"}]}'
        self.assertEqual(install.parse_port_binding(raw, 30080), 8080)

    def test_returns_none_when_container_port_absent(self):
        raw = '{"6443/tcp":[{"HostIp":"127.0.0.1","HostPort":"42617"}]}'
        self.assertIsNone(install.parse_port_binding(raw, 30080))

    def test_returns_none_for_null_bindings(self):
        self.assertIsNone(install.parse_port_binding("null", 30080))

    def test_returns_none_for_empty_object(self):
        self.assertIsNone(install.parse_port_binding("{}", 30080))

    def test_returns_none_for_malformed_json(self):
        self.assertIsNone(install.parse_port_binding("not json", 30080))

    def test_returns_none_for_empty_entries_list(self):
        raw = '{"30080/tcp":[]}'
        self.assertIsNone(install.parse_port_binding(raw, 30080))
```

- [ ] **Step 2: Run to verify the new tests fail**

Run: `python3 tests/test_install.py -v`
Expected: FAIL — `AttributeError: module 'install' has no attribute 'parse_port_binding'` (6 errors in `TestParsePortBinding`).

- [ ] **Step 3: Implement `parse_port_binding` and `get_current_port_mapping`**

In `install.py`, add near the top imports:
```python
import json
```
(add alongside the existing `import argparse` / `import os` / etc. block)

Then add, near `get_worker_nodes`:
```python
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
```

- [ ] **Step 4: Run to verify the tests pass**

Run: `python3 tests/test_install.py -v`
Expected: all tests PASS, including the 6 new `TestParsePortBinding` tests.

- [ ] **Step 5: Commit**

```bash
git add install.py tests/test_install.py
git commit -m "feat(install): inspect kind control-plane container's port bindings"
```

---

### Task 4: Wire port-mapping into `ensure_cluster` + `.env` + ingress NodePort pin

**Files:**
- Modify: `install.py` (`ensure_cluster`, `helm_deploy`, `main()`; add `resolve_local_port`; add `import tempfile`)
- Modify: `.env` and `.env.example` (add `LOCAL_PORT`)
- Modify: `tests/test_install.py` (add `TestResolveLocalPort`)

**Interfaces:**
- Consumes: `render_kind_config` (Task 2), `INGRESS_NODE_PORT` (Task 2), `get_current_port_mapping` (Task 3).
- Produces: `resolve_local_port(env_values: dict) -> int`, `ensure_cluster(local_port: int)` (signature change — was `ensure_cluster()`), `helm_deploy` gains the ingress NodePort pin flag (no signature change).

- [ ] **Step 1: Add the failing test for `resolve_local_port`**

In `tests/test_install.py`, add:
```python
class TestResolveLocalPort(unittest.TestCase):
    def test_uses_env_value(self):
        self.assertEqual(install.resolve_local_port({"LOCAL_PORT": "9090"}), 9090)

    def test_defaults_to_8080_when_unset(self):
        self.assertEqual(install.resolve_local_port({}), 8080)

    def test_exits_on_non_integer(self):
        with self.assertRaises(SystemExit):
            install.resolve_local_port({"LOCAL_PORT": "not-a-port"})
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 tests/test_install.py -v`
Expected: FAIL — `AttributeError: module 'install' has no attribute 'resolve_local_port'`.

- [ ] **Step 3: Implement `resolve_local_port`**

In `install.py`, add near `load_dotenv`:
```python
def resolve_local_port(env_values):
    raw = env_values.get("LOCAL_PORT", "8080")
    try:
        return int(raw)
    except ValueError:
        sys.exit("ERROR: LOCAL_PORT in .env must be an integer, got {!r}".format(raw))
```

- [ ] **Step 4: Run to verify it passes**

Run: `python3 tests/test_install.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Add `import tempfile` and rewrite `ensure_cluster`**

In `install.py`, add to the imports block:
```python
import tempfile
```

Replace the existing `ensure_cluster` function:
```python
def ensure_cluster():
    existing = subprocess.run(["kind", "get", "clusters"], capture_output=True, text=True).stdout.split()
    if CLUSTER_NAME in existing:
        print("kind cluster '{}' already exists, reusing.".format(CLUSTER_NAME))
        return
    run(["kind", "create", "cluster", "--name", CLUSTER_NAME,
         "--config", os.path.join(ROOT, "k8s", "kind-config.yaml")])
```
with:
```python
def ensure_cluster(local_port):
    existing = subprocess.run(["kind", "get", "clusters"], capture_output=True, text=True).stdout.split()
    if CLUSTER_NAME in existing:
        current_port = get_current_port_mapping()
        if current_port == local_port:
            print("kind cluster '{}' already exists, reusing (port {} already mapped).".format(
                CLUSTER_NAME, local_port))
            return
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
```

- [ ] **Step 6: Pin the ingress NodePort in `helm_deploy`**

In `install.py`, in `helm_deploy`, add `"--set", "ingress-nginx.controller.service.nodePorts.http=" + str(INGRESS_NODE_PORT),` to the base flags list (right after the `"upf-sim.enabled=..."` line, before `] + helm_set_flags_from_env(env_values) + [`):
```python
        "--set", "upf-sim.enabled=" + ("true" if include_generator else "false"),
        "--set", "ingress-nginx.controller.service.nodePorts.http=" + str(INGRESS_NODE_PORT),
    ] + helm_set_flags_from_env(env_values) + [
```

- [ ] **Step 7: Wire `resolve_local_port`/`ensure_cluster` into `main()`**

In `main()`, replace:
```python
    print("== 2/6 Ensuring kind cluster ==")
    ensure_cluster()
```
with:
```python
    print("== 2/6 Ensuring kind cluster ==")
    local_port = resolve_local_port(env_values)
    ensure_cluster(local_port)
```

Note: `env_values = load_dotenv()` already runs earlier in `main()` (before step "1/6"), so `env_values` is available here without moving anything.

- [ ] **Step 8: Add `LOCAL_PORT` to `.env.example` and `.env`**

In `.env.example`, add a new section right after the `# ── Ingress ──` section:
```
# Host port the frontend/API are reachable at (http://localhost:$LOCAL_PORT)
# after install.py finishes — no manual kubectl port-forward needed.
# Changing this recreates the kind cluster on the next install.py run.
LOCAL_PORT=8080
```

In `.env`, add the same key with the same value (this machine already uses 8080 as established in prior sessions):
```
LOCAL_PORT=8080
```

- [ ] **Step 9: Run the full test suite**

Run: `python3 tests/test_install.py -v`
Expected: all tests PASS.

- [ ] **Step 10: Live verification**

Run: `python3 install.py --skip-build`

Then:
```bash
curl -s -o /dev/null -w "HTTP %{http_code}\n" --max-time 5 http://localhost:8080/
```
Expected: `HTTP 200`, with no `kubectl port-forward` process running (verify with `pgrep -fa "port-forward"` returning nothing, or nothing relevant to this stack).

If the existing cluster was created before this change (no port mapping), the run should print `kind cluster 'bess-upf' exists but maps host port None (want 8080); recreating.` and proceed automatically — confirm this happened by checking the printed installer output, not just the final curl.

- [ ] **Step 11: Commit**

```bash
git add install.py .env .env.example tests/test_install.py
git commit -m "feat(install): native localhost access via kind extraPortMappings, no manual port-forward"
```

---

### Task 5: Fix the vLLM GPU resource key bug

**Files:**
- Modify: `charts/bess-upf/charts/analysis/templates/vllm.yaml:110-114`

**Interfaces:**
- Consumes: nothing from earlier tasks (independent chart fix).
- Produces: nothing consumed by later tasks — verified purely via `helm template`, but functionally required for Task 7's real-mode verification to mean anything.

- [ ] **Step 1: Confirm the bug via `helm template`**

Run:
```bash
cd charts/bess-upf
helm template bess-upf . -n bess-upf --set analysis.vllm.mock.enabled=false 2>&1 | grep -A1 "nvidia.com/gpu"
cd ../..
```
Expected: `nvidia.com/gpu: ""` (empty — confirms the bug: `.Values.vllm.resources.limits.gpu` doesn't exist, so `| quote` renders the empty default).

- [ ] **Step 2: Fix the template**

In `charts/bess-upf/charts/analysis/templates/vllm.yaml`, replace:
```yaml
          resources:
            limits:
              nvidia.com/gpu: {{ .Values.vllm.resources.limits.gpu | quote }}
            requests:
              nvidia.com/gpu: {{ .Values.vllm.resources.requests.gpu | quote }}
```
with:
```yaml
          resources:
            limits:
              nvidia.com/gpu: {{ index .Values.vllm.resources.limits "nvidia.com/gpu" | quote }}
            requests:
              nvidia.com/gpu: {{ index .Values.vllm.resources.requests "nvidia.com/gpu" | quote }}
```

- [ ] **Step 3: Verify the fix**

Run:
```bash
cd charts/bess-upf
helm template bess-upf . -n bess-upf --set analysis.vllm.mock.enabled=false 2>&1 | grep -A1 "nvidia.com/gpu"
cd ../..
```
Expected: `nvidia.com/gpu: "1"` (both limits and requests — matches `values.yaml`'s `nvidia.com/gpu: "1"` default).

- [ ] **Step 4: Verify mock mode (the default, and what's actually live) still renders unaffected**

Run:
```bash
cd charts/bess-upf
helm template bess-upf . -n bess-upf > /tmp/mock-render.yaml 2>&1
grep -c "mock_vllm.py" /tmp/mock-render.yaml
cd ../..
rm -f /tmp/mock-render.yaml
```
Expected: a positive count (mock deployment still renders — this fix only touches the real-mode branch, which is `{{- else }}` gated and doesn't affect mock's `{{- if .Values.vllm.mock.enabled }}` branch).

- [ ] **Step 5: Commit**

```bash
git add charts/bess-upf/charts/analysis/templates/vllm.yaml
git commit -m "fix(analysis): vllm real-mode GPU resource key never matched values.yaml, so it silently requested no GPU"
```

---

### Task 6: VRAM detection + tier resolution

**Files:**
- Modify: `install.py` (add `parse_nvidia_smi_output`, `detect_vram_mib`, `VLLM_TIERS`, `resolve_vllm_tier`, `resolve_vllm_config`)
- Modify: `.env` and `.env.example` (add `VLLM_MODE`, `VLLM_MODEL_OVERRIDE`)
- Modify: `tests/test_install.py` (add `TestParseNvidiaSmiOutput`, `TestResolveVllmTier`, `TestResolveVllmConfig`)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `detect_vram_mib() -> int | None`, `resolve_vllm_config(env_values: dict, vram_mib: int | None) -> dict` with keys `mock` (bool), `model` (str|None), `max_model_len` (int|None), `gpu_mem_util` (str|None). Both consumed by Task 7.

- [ ] **Step 1: Add the failing tests**

In `tests/test_install.py`, add:
```python
class TestParseNvidiaSmiOutput(unittest.TestCase):
    def test_single_gpu(self):
        self.assertEqual(install.parse_nvidia_smi_output("8188\n"), 8188)

    def test_multi_gpu_takes_max(self):
        self.assertEqual(install.parse_nvidia_smi_output("8188\n24576\n"), 24576)

    def test_empty_output(self):
        self.assertIsNone(install.parse_nvidia_smi_output(""))

    def test_garbage_output(self):
        self.assertIsNone(install.parse_nvidia_smi_output("not a number\n"))

    def test_mixed_garbage_and_numbers_ignores_garbage(self):
        self.assertEqual(install.parse_nvidia_smi_output("not a number\n16384\n"), 16384)


class TestResolveVllmTier(unittest.TestCase):
    def test_none_vram_is_none(self):
        self.assertIsNone(install.resolve_vllm_tier(None))

    def test_below_lowest_threshold_is_none(self):
        self.assertIsNone(install.resolve_vllm_tier(8191))

    def test_8188_mib_dev_laptop_is_none(self):
        # The exact boundary case this feature was designed around: an
        # "8GB" laptop GPU reports 8188 MiB, strictly under 8192.
        self.assertIsNone(install.resolve_vllm_tier(8188))

    def test_lower_tier_boundary(self):
        model, max_len, util = install.resolve_vllm_tier(8192)
        self.assertEqual(model, "Qwen/Qwen2.5-3B-Instruct-AWQ")
        self.assertEqual(max_len, 4096)
        self.assertEqual(util, "0.85")

    def test_upper_edge_of_lower_tier(self):
        model, _, _ = install.resolve_vllm_tier(16383)
        self.assertEqual(model, "Qwen/Qwen2.5-3B-Instruct-AWQ")

    def test_middle_tier_boundary(self):
        model, max_len, util = install.resolve_vllm_tier(16384)
        self.assertEqual(model, "Qwen/Qwen2.5-7B-Instruct-AWQ")
        self.assertEqual(max_len, 2048)
        self.assertEqual(util, "0.8")

    def test_upper_edge_of_middle_tier(self):
        model, _, _ = install.resolve_vllm_tier(24575)
        self.assertEqual(model, "Qwen/Qwen2.5-7B-Instruct-AWQ")

    def test_top_tier_boundary(self):
        model, max_len, util = install.resolve_vllm_tier(24576)
        self.assertEqual(model, "Qwen/Qwen2.5-14B-Instruct-AWQ")
        self.assertEqual(max_len, 4096)
        self.assertEqual(util, "0.85")

    def test_way_above_top_tier(self):
        model, _, _ = install.resolve_vllm_tier(81920)
        self.assertEqual(model, "Qwen/Qwen2.5-14B-Instruct-AWQ")


class TestResolveVllmConfig(unittest.TestCase):
    def test_mode_mock_forces_mock_regardless_of_vram(self):
        cfg = install.resolve_vllm_config({"VLLM_MODE": "mock"}, 81920)
        self.assertEqual(cfg, {"mock": True, "model": None, "max_model_len": None, "gpu_mem_util": None})

    def test_auto_with_no_gpu_is_mock(self):
        cfg = install.resolve_vllm_config({}, None)
        self.assertTrue(cfg["mock"])

    def test_auto_below_threshold_is_mock(self):
        cfg = install.resolve_vllm_config({"VLLM_MODE": "auto"}, 8188)
        self.assertTrue(cfg["mock"])

    def test_auto_picks_small_tier(self):
        cfg = install.resolve_vllm_config({}, 10000)
        self.assertEqual(cfg["mock"], False)
        self.assertEqual(cfg["model"], "Qwen/Qwen2.5-3B-Instruct-AWQ")

    def test_auto_picks_default_tier(self):
        cfg = install.resolve_vllm_config({}, 20000)
        self.assertEqual(cfg["model"], "Qwen/Qwen2.5-7B-Instruct-AWQ")

    def test_auto_picks_large_tier(self):
        cfg = install.resolve_vllm_config({}, 30000)
        self.assertEqual(cfg["model"], "Qwen/Qwen2.5-14B-Instruct-AWQ")

    def test_auto_with_override_keeps_tier_sizing_but_swaps_model(self):
        cfg = install.resolve_vllm_config({"VLLM_MODEL_OVERRIDE": "custom/model"}, 10000)
        self.assertEqual(cfg["model"], "custom/model")
        self.assertEqual(cfg["max_model_len"], 4096)  # small tier's sizing, per 10000 MiB
        self.assertEqual(cfg["gpu_mem_util"], "0.85")

    def test_real_mode_with_no_vram_falls_back_to_default_tier(self):
        cfg = install.resolve_vllm_config({"VLLM_MODE": "real"}, None)
        self.assertEqual(cfg["mock"], False)
        self.assertEqual(cfg["model"], "Qwen/Qwen2.5-7B-Instruct-AWQ")

    def test_real_mode_with_override_and_no_vram(self):
        cfg = install.resolve_vllm_config({"VLLM_MODE": "real", "VLLM_MODEL_OVERRIDE": "custom/model"}, None)
        self.assertEqual(cfg["model"], "custom/model")
        self.assertEqual(cfg["max_model_len"], 2048)  # fallback default tier's sizing

    def test_real_mode_respects_detected_vram_tier(self):
        cfg = install.resolve_vllm_config({"VLLM_MODE": "real"}, 30000)
        self.assertEqual(cfg["model"], "Qwen/Qwen2.5-14B-Instruct-AWQ")
```

- [ ] **Step 2: Run to verify the new tests fail**

Run: `python3 tests/test_install.py -v`
Expected: FAIL — `AttributeError` for `parse_nvidia_smi_output`, `resolve_vllm_tier`, `resolve_vllm_config` (all undefined so far).

- [ ] **Step 3: Implement the VRAM detection and tier resolution functions**

In `install.py`, add near `get_worker_nodes`:
```python
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
```

- [ ] **Step 4: Run to verify the tests pass**

Run: `python3 tests/test_install.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Add `.env.example` documentation**

In `.env.example`, add a new section after the `HF_TOKEN` section:
```
# ── vLLM model selection ───────────────────────────────────────────────────
# auto (default): install.py detects local VRAM via nvidia-smi and picks a
#   model automatically — <8GB or no GPU → mock, 8-16GB → Qwen2.5-3B-AWQ,
#   16-24GB → Qwen2.5-7B-AWQ, 24GB+ → Qwen2.5-14B-AWQ.
# mock: always use the mock vLLM (canned responses), ignore VRAM entirely.
# real: always use a real model — the tier VRAM would pick, or the 7B
#   default tier if VRAM can't be detected (e.g. deploying to a remote GPU
#   node pool install.py can't see locally).
VLLM_MODE=auto

# Force a specific model id instead of the tier-picked one. Only applies
# when the resolved mode is real (auto-with-GPU, or mode=real). Leave blank
# to use whichever model the tier/fallback picks.
VLLM_MODEL_OVERRIDE=
```

- [ ] **Step 6: Add the same keys to `.env`, matching this machine's actual situation**

In `.env`, add:
```
# This machine's GPU reports 8188 MiB — under the 8192 MiB threshold, so
# auto mode picks mock regardless. Leave as auto; set VLLM_MODE=real +
# VLLM_MODEL_OVERRIDE explicitly only when targeting a real GPU node pool.
VLLM_MODE=auto
VLLM_MODEL_OVERRIDE=
```

- [ ] **Step 7: Commit**

```bash
git add install.py .env .env.example tests/test_install.py
git commit -m "feat(install): VRAM-tiered vLLM model selection with mock fallback"
```

---

### Task 7: Wire vLLM resolution into the Helm deploy

**Files:**
- Modify: `install.py` (add `helm_set_flags_from_vllm`, thread `vllm_config` through `helm_deploy` and `main()`)
- Modify: `tests/test_install.py` (add `TestHelmSetFlagsFromVllm`)

**Interfaces:**
- Consumes: `resolve_vllm_config`, `detect_vram_mib` (Task 6).
- Produces: `helm_set_flags_from_vllm(vllm_config: dict) -> list[str]`. `helm_deploy` signature changes from `(tag, include_generator, timeout_min, env_values)` to `(tag, include_generator, timeout_min, env_values, vllm_config)`.

- [ ] **Step 1: Add the failing test**

In `tests/test_install.py`, add:
```python
class TestHelmSetFlagsFromVllm(unittest.TestCase):
    def test_mock_sets_only_mock_enabled_true(self):
        flags = install.helm_set_flags_from_vllm({"mock": True, "model": None, "max_model_len": None, "gpu_mem_util": None})
        self.assertEqual(flags, ["--set", "analysis.vllm.mock.enabled=true"])

    def test_real_sets_all_four_values(self):
        cfg = {"mock": False, "model": "Qwen/Qwen2.5-3B-Instruct-AWQ", "max_model_len": 4096, "gpu_mem_util": "0.85"}
        flags = install.helm_set_flags_from_vllm(cfg)
        self.assertIn("--set", flags)
        self.assertIn("analysis.vllm.mock.enabled=false", flags)
        self.assertIn("--set-string", flags)
        self.assertIn("analysis.vllm.model=Qwen/Qwen2.5-3B-Instruct-AWQ", flags)
        self.assertIn("analysis.vllm.maxModelLen=4096", flags)
        self.assertIn("analysis.vllm.gpuMemoryUtilization=0.85", flags)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 tests/test_install.py -v`
Expected: FAIL — `AttributeError: module 'install' has no attribute 'helm_set_flags_from_vllm'`.

- [ ] **Step 3: Implement `helm_set_flags_from_vllm`**

In `install.py`, add near `helm_set_flags_from_env`:
```python
def helm_set_flags_from_vllm(vllm_config):
    flags = ["--set", "analysis.vllm.mock.enabled=" + ("true" if vllm_config["mock"] else "false")]
    if not vllm_config["mock"]:
        flags += [
            "--set-string", "analysis.vllm.model=" + vllm_config["model"],
            "--set", "analysis.vllm.maxModelLen=" + str(vllm_config["max_model_len"]),
            "--set-string", "analysis.vllm.gpuMemoryUtilization=" + str(vllm_config["gpu_mem_util"]),
        ]
    return flags
```

- [ ] **Step 4: Run to verify it passes**

Run: `python3 tests/test_install.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Thread `vllm_config` through `helm_deploy`**

In `install.py`, replace the `helm_deploy` signature and body:
```python
def helm_deploy(tag, include_generator, timeout_min, env_values):
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
    ] + helm_set_flags_from_env(env_values) + [
        "--force",
        "--timeout", "{}m".format(timeout_min),
    ])
```
with:
```python
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
```

- [ ] **Step 6: Wire detection + resolution + the call site into `main()`**

In `main()`, replace:
```python
    print("== 5/6 Helm deploy ==")
    helm_deploy(args.tag, include_generator, args.timeout, env_values)
```
with:
```python
    vram_mib = detect_vram_mib()
    vllm_config = resolve_vllm_config(env_values, vram_mib)
    if vllm_config["mock"]:
        print("vLLM: mock mode (detected VRAM: {})".format(vram_mib if vram_mib is not None else "none/undetected"))
    else:
        print("vLLM: real mode, model={} (detected VRAM: {} MiB)".format(vllm_config["model"], vram_mib))

    print("== 5/6 Helm deploy ==")
    helm_deploy(args.tag, include_generator, args.timeout, env_values, vllm_config)
```

- [ ] **Step 7: Run the full test suite**

Run: `python3 tests/test_install.py -v`
Expected: all tests PASS (should be ~25+ tests across all `TestCase` classes at this point).

- [ ] **Step 8: Dry-run verify each tier renders correctly via `helm template`**

Run, from repo root:
```bash
cd charts/bess-upf

echo "--- mock (default) ---"
helm template bess-upf . -n bess-upf | grep -c "mock_vllm.py"

echo "--- 3B tier ---"
helm template bess-upf . -n bess-upf \
  --set analysis.vllm.mock.enabled=false \
  --set-string analysis.vllm.model=Qwen/Qwen2.5-3B-Instruct-AWQ \
  --set analysis.vllm.maxModelLen=4096 \
  --set-string analysis.vllm.gpuMemoryUtilization=0.85 \
  | grep -E "model|max-model-len|gpu-memory-utilization|nvidia.com/gpu" -A1

echo "--- 14B tier ---"
helm template bess-upf . -n bess-upf \
  --set analysis.vllm.mock.enabled=false \
  --set-string analysis.vllm.model=Qwen/Qwen2.5-14B-Instruct-AWQ \
  --set analysis.vllm.maxModelLen=4096 \
  --set-string analysis.vllm.gpuMemoryUtilization=0.85 \
  | grep -E "model|max-model-len|gpu-memory-utilization|nvidia.com/gpu" -A1

cd ../..
```
Expected: mock count > 0; both tier renders show the correct model string, `4096`, `0.85`, and `nvidia.com/gpu: "1"` (confirms Task 5's fix is load-bearing here).

- [ ] **Step 9: Commit**

```bash
git add install.py tests/test_install.py
git commit -m "feat(install): wire VRAM-tiered vllm config into helm deploy"
```

---

### Task 8: End-to-end live verification + final docs pass

**Files:**
- Modify: `README.md` (mention VRAM-based vLLM selection alongside the existing vLLM/RUNBOOK pointer)
- No code changes — this task is verification plus a doc note.

**Interfaces:** None (terminal task).

- [ ] **Step 1: Run the full test suite one more time**

Run: `python3 tests/test_install.py -v`
Expected: all tests PASS.

- [ ] **Step 2: Live end-to-end run against the real cluster (default flags — no `--generator`)**

Run: `python3 install.py --skip-build`

Expected console output includes, in order:
- `Loaded N value(s) from .env`
- `== 2/6 Ensuring kind cluster ==` — either "already exists, reusing (port 8080 already mapped)" if Task 4's live check already recreated it, or a fresh recreate line if `.env` changed since.
- `vLLM: mock mode (detected VRAM: ...)` — this machine's 8188 MiB is under threshold, so mock is expected here.
- Helm upgrade succeeds.
- `== 6/6 Waiting for stack ==` ends with all pods healthy.

- [ ] **Step 3: Confirm the generator is absent**

Run: `kubectl get pods -n bess-upf -l app=upf-sim`
Expected: `No resources found in bess-upf namespace.`

- [ ] **Step 4: Confirm localhost access still works with no manual port-forward**

Run:
```bash
pgrep -fa "kubectl port-forward" || echo "no port-forward process running"
curl -s -o /dev/null -w "HTTP %{http_code}\n" --max-time 5 http://localhost:8080/
curl -s -o /dev/null -w "HTTP %{http_code}\n" --max-time 5 http://localhost:8080/api/v1/health
```
Expected: `no port-forward process running`, then `HTTP 200` for `/`, then `HTTP 401` for `/api/v1/health` (same auth-gated-but-real-routing signature confirmed earlier in this project).

- [ ] **Step 5: Confirm vLLM is genuinely running mock (matches the detected tier)**

Run: `kubectl get deployment -n bess-upf vllm -o jsonpath='{.spec.template.spec.containers[0].image}'`
Expected: `python:3.11-slim` (the mock image) — confirms `resolve_vllm_config` correctly kept this machine on mock given its 8188 MiB GPU.

- [ ] **Step 6: Add a short README note on vLLM model selection**

In `README.md`, find the existing line:
```markdown
- `vllm` — LLM backend for `analysis`'s chat feature. Runs in **mock mode** by default (canned responses) since it needs a real GPU node otherwise (CUDA image, no CPU fallback) — see [RUNBOOK.md](./RUNBOOK.md).
```
Replace with:
```markdown
- `vllm` — LLM backend for `analysis`'s chat feature. `install.py` auto-detects local VRAM (via `nvidia-smi`) and picks mock (<8GB or no GPU), a 3B, 7B, or 14B AWQ model accordingly — see `.env.example`'s `VLLM_MODE`/`VLLM_MODEL_OVERRIDE` and [RUNBOOK.md](./RUNBOOK.md).
```

- [ ] **Step 7: Commit**

```bash
git add README.md
git commit -m "docs: note VRAM-based vLLM model selection in README"
```

---

## Self-Review Notes

- **Spec coverage:** Generator opt-in (Task 1), native port mapping via `extraPortMappings` + auto-recreate (Tasks 2–4), GPU resource key bug fix (Task 5), VRAM detection + tier resolution + `.env` escape hatch (Task 6), wiring into `helm_deploy` (Task 7), live end-to-end verification (Task 8) — all spec sections covered.
- **Placeholder scan:** No TBD/TODO; every step has complete code or an exact command with expected output.
- **Type consistency:** `render_kind_config`, `INGRESS_NODE_PORT`, `get_current_port_mapping`, `resolve_local_port`, `detect_vram_mib`, `resolve_vllm_config`, `helm_set_flags_from_vllm` are used with identical names/signatures everywhere they're referenced across tasks.
