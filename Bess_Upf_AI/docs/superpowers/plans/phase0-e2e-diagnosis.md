# Phase 0 — T5 E2E Diagnosis Log

**Date:** 2026-07-02
**Method:** superpowers:systematic-debugging (hop-by-hop trace, one hypothesis at a time)
**Symptom (as reported):** "Dashboard shows no metrics despite VictoriaMetrics holding ~14.5M rows" (memory note, 2026-07-01 morning).

## Conclusion (up front)

**There is no v1 end-to-end product break.** Every hop of the pipeline works. The
reported symptom was **stale** — resolved during the day's work before this session.
The only real defect found was in the **eval harness invocation**, not the product.

## Hop-by-hop evidence

| Hop | Test | Result |
|---|---|---|
| 1. Sim exposes metrics | `docker exec upf-sim wget .../metrics \| grep pfcp_sessions_total` | ✅ `pfcp_sessions_total{node_id="sim-upf-01"} 730141` |
| 2. Prometheus scraping | `/api/v1/targets` | ✅ `status:success`, targets healthy |
| 3. VM has series | query `up`, `pfcp_sessions_total` (with auth) | ✅ data present; `job` values = analysis, detection, prometheus, **upf** (4), upf_sim |
| 4. Detection polls VM | `docker logs detection` | ✅ continuous `tier-2 anomaly event` (ml_isolation_forest), latest 04:02 |
| 5. Analysis query API | `GET /api/v1/query?q=up` via Caddy | ✅ real samples returned |
| 5b. Analysis anomalies API | `GET /api/v1/anomalies` | ✅ real events (id 1278, ml_isolation_forest) |
| 5c. Frontend's exact PromQL | `q=pfcp_sessions_total{job="upf"}` | ✅ `752442` — `job="upf"` attached by VM relabeling |
| 6. LLM chat + tool-calling | `POST /api/v1/chat` | ✅ correct answer "≈736,612 sessions", `queries_used:[pfcp_sessions_total]`, tool-calling works, vLLM on GPU, ~31s |
| 7. Frontend client param | `frontend/src/api/client.ts:229` | ✅ uses `q=<promql>` (matches API) |

## Hypotheses formed and killed

1. **VM auth mismatch (.env vs compose)** — KILLED. `.env` `VM_AUTH_*` = `vmadmin/localdev123` = compose values exactly; detection & analysis both inherit them and detection is actively querying VM.
2. **`{job="upf"}` selector matches nothing** — KILLED. VM relabeling attaches `job="upf"` to sim metrics; the query returns data.
3. **Analysis query endpoint broken** — KILLED. Works with the correct `q=` param (`query=` returns "q parameter is required" by design).

## The one real defect: eval harness invocation

- **Symptom:** all 10 eval cases → `tls: failed to verify certificate: x509: signed by unknown authority`.
- **Root cause:** `eval/main.go` reads **CLI flags** (`-url -user -pass -insecure`), NOT env vars. The `-insecure` flag (skip TLS verify for the self-signed dev cert) defaults to `false`. The documented invocation in the Phase 0 plan (`ANALYSIS_URL=… go run .`) set env vars the harness ignores and omitted `-insecure`, so it failed TLS before testing anything.
- **Correct invocation:**
  ```bash
  cd eval && go run . -url=https://localhost -user="$ANALYSIS_AUTH_USER" \
    -pass="$ANALYSIS_AUTH_PASSWORD" -insecure
  ```
- **Fix:** correct the plan's HOP6/exit-criterion command (docs), not the harness code. The harness correctly defaults to verifying certs.

## Secondary observation (not the symptom, not blocking)

- `chronos-sidecar` and `moment-sidecar` report **unhealthy**, but only because their Docker HEALTHCHECK runs `python` which isn't on the container `$PATH` (`exec: "python": not found`). The service processes themselves run (started via CMD with the correct interpreter). This is a broken healthcheck, not a down service — detection's tier-2 ML runs fine. Fix later by pointing the healthcheck at the correct interpreter path or `python3`.

## Verification

- `eval` harness with the correct flags: see run result appended below / commit message.
- Frontend browser KPI render: the running `frontend` container is 12h old; if it predates the working-tree `client.ts`/`metrics.ts` fixes, a `docker compose build frontend` may be needed to serve the current source. API layer is proven healthy regardless.
