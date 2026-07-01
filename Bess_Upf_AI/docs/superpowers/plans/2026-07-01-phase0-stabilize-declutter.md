# Phase 0 — Stabilize & De-clutter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Get the repo to a clean, CI-verified, known-good baseline before any v2 streaming work begins.

**Architecture:** No new services. Remove junk, move large artifacts out of Git LFS, split the 1010-line compose, add CI, and diagnose+fix the v1 end-to-end metrics/LLM gap so there is a trustworthy baseline to shadow the future v2 detection against.

**Tech Stack:** Git, Git LFS, Docker Compose, GitHub Actions, Go, Python (ruff), Node/Vite.

**Parent spec:** `docs/superpowers/specs/2026-07-01-bess-upf-v2-rebuild-design.md` (Phase 0).

---

## File map

- Delete: `analysis;C/`, `ansh_out_15_06_2026.csv/`, `prometheus_full_export_20260622_115327.csv/`, `chronos_train.log`, `moment_train.log`
- Modify: `.gitignore` (add logs), `.gitattributes` (stop LFS-tracking model artifacts), `run.sh` + `run.ps1` (replace `git lfs pull` with MinIO fetch)
- Create: `.github/workflows/ci.yml`, `scripts/fetch-models.sh`, `docker-compose.base.yml` + `docker-compose.core.yml` + `docker-compose.ml.yml` (split of monolith), `docs/superpowers/plans/phase0-e2e-diagnosis.md` (debug log)
- Modify: `docker-compose.yml` (reduce to an include/override shim or remove after split)

---

## Task 1: Remove stray junk from repo root

**Files:**
- Delete: `analysis;C/`, `ansh_out_15_06_2026.csv/`, `prometheus_full_export_20260622_115327.csv/`, `chronos_train.log`, `moment_train.log`
- Modify: `.gitignore`

- [ ] **Step 1: Confirm none are git-tracked**

Run: `git ls-files | grep -E 'analysis;C|ansh_out|prometheus_full_export|chronos_train.log|moment_train.log'`
Expected: empty output (all untracked). If any line prints, stop and inspect before deleting.

- [ ] **Step 2: Delete the junk**

```bash
rm -rf "analysis;C" "ansh_out_15_06_2026.csv" "prometheus_full_export_20260622_115327.csv"
rm -f chronos_train.log moment_train.log
```

- [ ] **Step 3: Add log patterns to .gitignore**

Append to `.gitignore`:

```
# Training / working logs
*.log
```

- [ ] **Step 4: Verify clean**

Run: `git status --porcelain | grep -E 'analysis;C|ansh_out|prometheus_full_export|\.log$'`
Expected: empty output.

- [ ] **Step 5: Commit**

```bash
git add .gitignore
git commit -m "chore: remove stray junk dirs and root logs; ignore *.log"
```

---

## Task 2: Move model artifacts out of Git LFS into MinIO

**Rationale:** Carrier-grade puts model weights in an object store / registry, not the git repo. LFS clones are 1.2GB+ and couple code history to weights.

**Files:**
- Modify: `.gitattributes`, `run.sh` (the `git lfs pull` step), `run.ps1` (the `git lfs pull` step)
- Create: `scripts/fetch-models.sh`

- [ ] **Step 1: Locate the LFS pull step in run scripts**

Run: `grep -n "lfs pull" run.sh run.ps1`
Expected: one match in each. Note the line numbers.

- [ ] **Step 2: Write `scripts/fetch-models.sh`**

```bash
#!/usr/bin/env bash
# Fetch model artifacts from MinIO instead of Git LFS.
# Requires: mc (MinIO client); MODEL_BUCKET + MODEL_ENDPOINT + MinIO creds in env.
set -euo pipefail
: "${MODEL_BUCKET:?set MODEL_BUCKET}"
: "${MODEL_ENDPOINT:?set MODEL_ENDPOINT}"
DEST="${1:-models}"
mkdir -p "$DEST" tools/train/data
echo "Fetching model artifacts from ${MODEL_ENDPOINT}/${MODEL_BUCKET} ..."
mc alias set modelsrc "$MODEL_ENDPOINT" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
mc cp --recursive "modelsrc/${MODEL_BUCKET}/models/" "$DEST/"
echo "Done."
```

- [ ] **Step 3: Upload current artifacts to MinIO once (manual bootstrap)**

Run (with the stack's MinIO up):
```bash
mc cp --recursive models/ "modelsrc/${MODEL_BUCKET}/models/"
```
Expected: all 6 artifacts uploaded. Verify: `mc ls --recursive "modelsrc/${MODEL_BUCKET}/models/"`.

- [ ] **Step 4: Stop LFS-tracking; keep artifacts local + gitignored**

Replace `.gitattributes` contents with:

```
# Model + dataset artifacts are fetched from MinIO (scripts/fetch-models.sh), not Git LFS.
```

Append to `.gitignore`:

```
# Model + dataset artifacts (fetched from MinIO, not committed)
models/*.joblib
models/*.pt
tools/train/data/*.parquet
tools/train/data/*.npz
```

- [ ] **Step 5: Untrack the artifacts from git (keep on disk)**

```bash
git rm --cached models/random_forest.joblib \
  tools/train/data/chronos_train.parquet \
  tools/train/data/chronos_zeroshot_cache.npz \
  tools/train/data/moment_embeddings_cache.npz \
  tools/train/data/moment_windows.npz \
  tools/train/data/synthetic_dataset.parquet
```

- [ ] **Step 6: Swap the run-script pull step**

In `run.sh` and `run.ps1`, replace the `git lfs pull ...` line with a call to `scripts/fetch-models.sh` (bash) / an equivalent `mc cp` block (PowerShell). Keep it non-fatal with a clear warning if MinIO is unreachable, matching the existing error-handling style in those scripts.

- [ ] **Step 7: Verify pack size does not grow**

Run: `git count-objects -vH | grep size-pack`
Expected: no new multi-GB blobs added. LFS history remains but is no longer grown.

- [ ] **Step 8: Commit**

```bash
git add .gitattributes .gitignore scripts/fetch-models.sh run.sh run.ps1
git commit -m "chore: fetch model artifacts from MinIO instead of Git LFS"
```

---

## Task 3: Split the 1010-line docker-compose.yml by responsibility

**Files:**
- Create: `docker-compose.base.yml` (networks, volumes)
- Create: `docker-compose.core.yml` (victoriametrics, minio, minio-init, caddy, frontend, analysis, detection, export-job)
- Create: `docker-compose.ml.yml` (vllm, ml-infer, moment/chronos/stl sidecars)
- Modify: `docker-compose.yml` → thin shim or delete after callers updated

> Note: this split preserves the *current v1 baseline* so Task 5 can validate it. v2 services stay in `docker-compose.v2.yml`.

- [ ] **Step 1: Inventory services in the monolith**

Run: `grep -nE '^  [a-z].*:$' docker-compose.yml`
Expected: full service list with line numbers. Record which lines belong to core vs ml.

- [ ] **Step 2: Extract shared `networks:` + `volumes:` into `docker-compose.base.yml`**

Copy the top-level `networks:` and `volumes:` blocks verbatim into `docker-compose.base.yml` under `version: "3.8"`. Remove them from the originals.

- [ ] **Step 3: Move core services into `docker-compose.core.yml`**

Cut the core service blocks (victoriametrics, minio, minio-init, caddy, frontend, analysis, detection, export-job) into `docker-compose.core.yml`. Preserve exact indentation, env, healthchecks.

- [ ] **Step 4: Move ML services into `docker-compose.ml.yml`**

Cut vllm, ml-infer, moment-sidecar, chronos-sidecar, stl-sidecar into `docker-compose.ml.yml`.

- [ ] **Step 5: Verify the split composes identically**

Run:
```bash
docker compose -f docker-compose.base.yml -f docker-compose.core.yml -f docker-compose.ml.yml config > /tmp/split.yml
git stash && docker compose -f docker-compose.yml config > /tmp/mono.yml && git stash pop
diff <(yq -S . /tmp/mono.yml) <(yq -S . /tmp/split.yml)
```
Expected: no semantic diff (ordering-only differences acceptable).

- [ ] **Step 6: Update run.sh / run.ps1 compose invocations to the three -f files**

Replace `-f docker-compose.yml` with `-f docker-compose.base.yml -f docker-compose.core.yml -f docker-compose.ml.yml` in both run scripts.

- [ ] **Step 7: Commit**

```bash
git add docker-compose.base.yml docker-compose.core.yml docker-compose.ml.yml docker-compose.yml run.sh run.ps1
git commit -m "refactor: split monolithic compose into base/core/ml files"
```

---

## Task 4: Add CI (build + test + lint gate)

**Files:**
- Create: `.github/workflows/ci.yml`

- [ ] **Step 1: Write the workflow**

```yaml
name: ci
on:
  push: { branches: ["**"] }
  pull_request:
jobs:
  go:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        svc: [analysis, detection, export-job, upf-sim]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-go@v5
        with: { go-version: "1.22" }
      - name: build + vet + test
        working-directory: ${{ matrix.svc }}
        run: |
          go build ./...
          go vet ./...
          go test ./... -count=1
  python:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install ruff
      - run: ruff check tools pipeline serve mitigation
  frontend:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: "20" }
      - working-directory: frontend
        run: |
          npm ci
          npm run build
```

- [ ] **Step 2: Dry-run each job command locally**

Run: `cd analysis && go build ./... && go vet ./... && go test ./... -count=1`
Expected: PASS (or record real failures as follow-up — do not fabricate a pass).
Run: `ruff check tools pipeline serve mitigation`
Run: `cd frontend && npm ci && npm run build`

- [ ] **Step 3: Fix any breakage the CI commands surface**

For each failure, fix minimally so the gate is green. If a failure is pre-existing and large, quarantine it (skip with a tracked `// TODO(phase0-ci): <issue>` and a note in the debug log) rather than block the whole phase.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: add go build/vet/test, ruff, frontend build gate"
```

- [ ] **Step 5: Push and confirm the workflow goes green**

Run: `git push`
Expected: Actions run passes on all three jobs. Fix until green.

---

## Task 5: Diagnose & fix the v1 end-to-end metrics/LLM gap

**This is a debugging task, not a pre-written code change.** Use the **superpowers:systematic-debugging** skill. Do NOT guess-patch. Produce a written root-cause before fixing.

**Files:**
- Create: `docs/superpowers/plans/phase0-e2e-diagnosis.md` (running debug log: hypothesis → test → result)

- [ ] **Step 1: Reproduce — bring up the baseline stack**

```bash
bash run.sh --dev
```
Wait for healthy. Record healthy/unhealthy containers: `docker compose ps`.

- [ ] **Step 2: Trace the pipeline hop by hop, writing each result to the debug log**

The FIRST hop that returns empty/zero is the break:
```bash
# 1. Sim exposes metrics?
docker compose exec upf-sim wget -qO- http://localhost:8090/metrics | grep pfcp_sessions_total | head
# 2. Prometheus scraping sim?
curl -s http://localhost:9090/api/v1/targets | jq '.data.activeTargets[] | {job:.labels.job, health}'
# 3. remote_write reaching VM? (VM has the series)
curl -s -u "$VM_AUTH_USERNAME:$VM_AUTH_PASSWORD" 'http://localhost:8428/api/v1/query?query=pfcp_sessions_total' | jq '.data.result | length'
# 4. Detection polling VM?
docker compose logs detection --tail=50 | grep -iE 'detection pass|error'
# 5. Analysis reaching VM + LLM?
curl -sk -u "$ANALYSIS_AUTH_USER:$ANALYSIS_AUTH_PASSWORD" https://localhost/api/v1/query?q=up | jq .
docker compose logs analysis --tail=50 | grep -iE 'error|vllm|ollama'
# 6. Frontend reaching analysis?
curl -sk -u "$ANALYSIS_AUTH_USER:$ANALYSIS_AUTH_PASSWORD" https://localhost/api/v1/anomalies | jq '. | length'
```

- [ ] **Step 3: Form ONE hypothesis at the first broken hop and write it in the debug log**

State the smallest thing that, if true, explains the empty result (e.g. "VM query empty because remote_write auth 401s" or "analysis 500s because vLLM never became ready on CPU").

- [ ] **Step 4: Test the hypothesis with a single targeted command**

Run the one command that confirms or kills it. Record result. If killed, return to Step 3 with the next hypothesis. Do not fix until a hypothesis is confirmed.

- [ ] **Step 5: Apply the minimal root-cause fix**

Fix at the shared root, not the symptom. Grep for other callers of whatever you change so sibling paths are fixed too.

- [ ] **Step 6: Verify E2E green**

```bash
cd eval && ANALYSIS_URL=https://localhost ANALYSIS_USER="$ANALYSIS_AUTH_USER" \
  ANALYSIS_PASSWORD="$ANALYSIS_AUTH_PASSWORD" go run .
```
Expected: `4/4 PASS`. Also confirm the frontend Overview shows live KPI values in a browser.

- [ ] **Step 7: Commit the fix + the diagnosis log**

```bash
git add -A
git commit -m "fix: restore v1 end-to-end metrics/LLM flow (root cause: <one line>)"
```

---

## Phase 0 exit criteria

- [ ] Repo root clean; `*.log` and artifacts gitignored; artifacts fetched from MinIO.
- [ ] `docker compose config` semantically identical after the split.
- [ ] CI green on go/python/frontend.
- [ ] `eval` harness returns `4/4 PASS`; frontend shows live data.
- [ ] `phase0-e2e-diagnosis.md` records the root cause.

On completion: start the **Phase 1** spec→plan cycle (real streaming backbone + ClickHouse sink).

---

## Self-review notes

- **Spec coverage (Phase 0 section):** junk removal ✔ (T1), LFS→object store ✔ (T2), compose split ✔ (T3), CI ✔ (T4), fix v1 E2E baseline ✔ (T5). All Phase 0 spec items mapped.
- **Placeholder scan:** T5 intentionally has no fabricated fix code — it is a debugging procedure with real verification commands (correct; root cause is unknown until reproduced). The only deferred item is the deliberate CI-quarantine escape hatch in T4S3.
- **Consistency:** `scripts/fetch-models.sh`, the three compose files, and `ci.yml` job names are referenced identically across tasks and the file map.
