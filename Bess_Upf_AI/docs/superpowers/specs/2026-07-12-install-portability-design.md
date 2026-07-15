# install.py portability: generator default, native port access, VRAM-tiered vLLM

Date: 2026-07-12
Status: Approved

## Context

`install.py` is the one-shot installer for the CoreWatch v2 stack (`kind` +
Helm). Four gaps came up while getting the stack running locally:

1. It defaults to deploying `upf-sim` (the synthetic traffic generator),
   which is a dev-only convenience — most environments (test, deployment)
   are fed by a real UPF and shouldn't get a generator unless asked for.
2. There is no built-in way to reach the frontend/API from `localhost`.
   The `kind` cluster's `k8s/kind-config.yaml` has no `extraPortMappings`,
   so the ingress-nginx NodePort is only reachable via a Docker-bridge node
   IP, or by a manually-run `kubectl port-forward` that has to be
   re-started by hand every session.
3. `analysis`'s vLLM backend only ever runs in mock mode or hard-codes a
   single real-GPU model (`Qwen/Qwen2.5-7B-Instruct-AWQ` at a fixed
   `maxModelLen`/`gpuMemoryUtilization`), regardless of what GPU (if any)
   is actually available.
4. The install path assumes whoever runs it has an NVIDIA GPU in reach.
   Test/deployment hardware may have no GPU, a small GPU, or a large one,
   and the script should behave sanely in all of those without erroring.

## Goals

- `install.py` deploys without `upf-sim` unless `--generator` is passed.
- After `install.py` completes, `http://localhost:$LOCAL_PORT` (default
  `8080`) reaches the frontend/API with no manual `kubectl port-forward`
  step, via `kind`'s native `extraPortMappings`, configurable through
  `.env`.
- vLLM's model choice is driven by detected VRAM, with mock as the safe
  fallback whenever detection is impossible or VRAM is below threshold.
- None of this errors out or requires an NVIDIA GPU to be present — a
  CPU-only or non-NVIDIA box degrades cleanly to mock + no port-mapping
  churn (port-mapping works regardless of GPU).

## Non-goals

- Making `kind` nodes actually schedule pods onto a GPU (NVIDIA Container
  Toolkit + Kubernetes device plugin inside `kind`). The chart's real vLLM
  path already assumes "a node pool with real GPU capacity" as a
  precondition (see RUNBOOK.md §4) — this work only makes the *model
  selection* correct and portable, not GPU passthrough into `kind` itself.
- ARM/non-x86 build support (`docker buildx` multi-arch). Confirmed out of
  scope — only GPU/no-GPU fallback is in scope for "universally runnable."
- Changing any port other than the browser-facing ingress port. Internal
  service ports already have a narrower, separately-scoped mechanism
  (`.env`'s `MITIGATION_API_PORT`, from the prior single-`.env` work).

## Design

### 1. Generator opt-in

`install.py`'s `argparse` setup replaces `--no-generator` (`store_true`,
generator on by default) with `--generator` (`store_true`, generator off
by default). `include_generator = args.generator`. README's Quick Start
section is updated to show the new default and flag name.

### 2. Native port access via `kind extraPortMappings`

- New `.env` key: `LOCAL_PORT` (default `8080`).
- Ingress-nginx's NodePort is pinned to a fixed value instead of today's
  random assignment, via
  `--set-string ingress-nginx.controller.service.nodePorts.http=30080`
  (added to `ENV_TO_HELM`-style handling, always applied — not
  conditional on `.env`, since the mapping requires a stable target).
- `install.py` gains `render_kind_config(local_port)`, which returns the
  same node/`extraMounts` structure `k8s/kind-config.yaml` has today, plus
  an `extraPortMappings` block on the control-plane node:
  ```yaml
  extraPortMappings:
    - containerPort: 30080
      hostPort: <local_port>
      protocol: TCP
  ```
  This is written to a temp file and passed to `kind create cluster
  --config <tmp>`. The checked-in `k8s/kind-config.yaml` gets the same
  block hardcoded at the default port (8080), so a manual `kind create
  cluster --config k8s/kind-config.yaml` (bypassing `install.py`) stays
  consistent with the documented default.
- Before creating/reusing the cluster, `install.py` checks the existing
  control-plane container's Docker port bindings (`docker inspect
  bess-upf-control-plane`, `HostConfig.PortBindings["30080/tcp"]`). Three
  cases:
  - No cluster exists → create fresh with the desired mapping.
  - Cluster exists, binding matches `.env`'s current `LOCAL_PORT` → reuse
    as-is (existing idempotent behavior).
  - Cluster exists, binding missing or points at a different host port →
    `kind delete cluster --name bess-upf`, then create fresh. This is the
    same recovery operation RUNBOOK.md §6.2 already documents as routine,
    just triggered automatically instead of by hand.
- Result: no `kubectl port-forward` process for anyone to start, track, or
  clean up. `localhost:$LOCAL_PORT` works immediately after `install.py`
  finishes, every time, on any machine that ran it.

### 3. VRAM-tiered vLLM model selection

- `install.py` gains `detect_vram_mib()`:
  - Runs `nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits`.
  - Missing binary, non-zero exit, unparseable output, or zero GPUs → `None`.
  - Multiple GPUs → take the max reported value (best-effort; a
    heterogeneous multi-GPU box is an edge case, not a target).
- Tier table (strict MiB thresholds):

  | VRAM | Mode | Model | maxModelLen | gpuMemoryUtilization |
  |---|---|---|---|---|
  | `None` or `< 8192` | mock | — | — | — |
  | `8192–16383` | real | `Qwen/Qwen2.5-3B-Instruct-AWQ` | 4096 | 0.85 |
  | `16384–24575` | real | `Qwen/Qwen2.5-7B-Instruct-AWQ` (today's default) | 2048 | 0.8 |
  | `>= 24576` | real | `Qwen/Qwen2.5-14B-Instruct-AWQ` | 4096 | 0.85 |

  All AWQ-quantized, for consistent memory-footprint behavior across tiers.
- `.env` escape hatch, read by `install.py`, independent of local
  detection:
  - `VLLM_MODE` = `auto` (default) | `mock` | `real`.
  - `VLLM_MODEL_OVERRIDE` — when set, used verbatim as the model id
    instead of the tier-picked one (only meaningful when the resolved mode
    is `real`).
  - This exists because `install.py` detects VRAM on the machine *it*
    runs on, which is correct for `kind` (host == node) but may not be for
    a deployment pipeline targeting a separate GPU node pool. The override
    lets that pipeline state its target explicitly rather than rely on
    local detection.
- Resolution order: `VLLM_MODE=mock` forces mock. `VLLM_MODE=real` forces
  real (using `VLLM_MODEL_OVERRIDE` if set, else the 16-24GB default tier
  model as a reasonable baseline). `VLLM_MODE=auto` (default) uses
  `detect_vram_mib()` and the tier table, with `VLLM_MODEL_OVERRIDE` (if
  set) substituted in place of whatever model the tier would have picked.
- These resolve to extra `--set`/`--set-string` flags on the `helm
  upgrade`: `analysis.vllm.mock.enabled`, `analysis.vllm.model`,
  `analysis.vllm.maxModelLen`, `analysis.vllm.gpuMemoryUtilization`.

### 4. Bug fix required for real mode to work at all

`charts/bess-upf/charts/analysis/templates/vllm.yaml` currently has:
```yaml
resources:
  limits:
    nvidia.com/gpu: {{ .Values.vllm.resources.limits.gpu | quote }}
```
but `values.yaml` defines the resource key as the literal string
`nvidia.com/gpu` (YAML doesn't nest on dots), not a nested `gpu` field
under `limits`. `.Values.vllm.resources.limits.gpu` is always empty,
silently rendering `nvidia.com/gpu: ""` — real mode has never actually
requested a GPU. Fix: use `index` to look up the literal key:
```yaml
resources:
  limits:
    nvidia.com/gpu: {{ index .Values.vllm.resources.limits "nvidia.com/gpu" | quote }}
  requests:
    nvidia.com/gpu: {{ index .Values.vllm.resources.requests "nvidia.com/gpu" | quote }}
```
This can only be verified by template-rendering (`helm template` with
`--set analysis.vllm.mock.enabled=false`), not live execution — no
machine in this loop has ≥8192 MiB VRAM under the confirmed strict
threshold. That limitation will be stated plainly when reporting
verification, not glossed over.

## Testing

- `helm template` with no overrides → byte-identical to current rendered
  output for everything except the now-pinned ingress NodePort (expected
  diff) and the GPU `index` fix (expected diff, previously-empty value now
  populated even in mock mode's unused real-mode branch).
- `helm template --set analysis.vllm.mock.enabled=false --set
  analysis.vllm.model=... --set analysis.vllm.maxModelLen=... --set
  analysis.vllm.gpuMemoryUtilization=...` for each of the three real tiers
  → confirms `nvidia.com/gpu` limit/request render non-empty and the vLLM
  container args carry the right model/length/util per tier.
- `install.py`'s new pure functions (`detect_vram_mib` parsing logic,
  tier-resolution logic, `render_kind_config`, port-binding-mismatch
  logic) get exercised directly via `python3 -c` against captured/fake
  `nvidia-smi` and `docker inspect` output — no live GPU or cluster
  recreation required to validate the branching.
- One live end-to-end run of `python install.py` (default, no
  `--generator`) against the current cluster, confirming: `upf-sim` is
  absent, `localhost:$LOCAL_PORT` serves the frontend with no manual
  port-forward, and mock vLLM still runs (since this machine's VRAM is
  under the real-mode threshold either way).

## Open risk

Recreating the `kind` cluster on a `LOCAL_PORT` change is destructive to
anything not captured by Helm/PVCs (e.g. in-flight Kafka topic data,
which RUNBOOK.md already notes has no Tiered Storage and just backfills
from the simulator/real UPF). This only fires when `LOCAL_PORT` actually
changes from the cluster's current mapping, not on ordinary re-runs.
