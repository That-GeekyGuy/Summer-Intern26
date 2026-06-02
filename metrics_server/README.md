# UPF Metrics Stack

A containerised observability stack for a 5G User Plane Function (UPF). It collects Prometheus-format metrics, stores them in Prometheus, evaluates recording rules and firing alerts, visualises everything in Grafana, exposes a filterable JSON query API, runs a Go polling worker, and serves a live browser dashboard — all as separate Docker containers.

---

## Table of Contents

1. [Architecture](#architecture)
2. [Repository Layout](#repository-layout)
3. [Services](#services)
   - [metrics-exporter](#1-metrics-exporter--port-8080)
   - [prometheus](#2-prometheus--port-9090)
   - [grafana](#3-grafana--port-3001)
   - [query-api](#4-query-api--port-8090)
   - [go-worker](#5-go-worker)
   - [frontend](#6-frontend--port-3000)
4. [Recording Rules & Alerts](#recording-rules--alerts)
5. [Traffic Simulation](#traffic-simulation)
6. [Quick Start](#quick-start)
7. [Query API Reference](#query-api-reference)
8. [Dashboard Guide](#dashboard-guide)
9. [Go Worker Flags](#go-worker-flags)
10. [Configuration Reference](#configuration-reference)
11. [Port Map](#port-map)
12. [Running Locally Without Docker](#running-locally-without-docker)
13. [How the Prometheus Parser Works](#how-the-prometheus-parser-works)

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                            Docker network                                │
│                                                                          │
│  ┌─────────────────┐   scrape /metrics (5 s)   ┌──────────────────────┐ │
│  │ metrics-exporter│◄──────────────────────────│      prometheus      │ │
│  │  Python :8080   │                            │   prom/prometheus    │ │
│  │  live simulator │   evaluate rules (5 s)     │   :9090             │ │
│  │  port_bytes_*   │   recording rules → TSDB   │   rules.yml         │ │
│  └────────┬────────┘   alert rules → ALERTS     └──────────┬──────────┘ │
│           │                                                │             │
│           │ fetch /metrics                        PromQL  │             │
│           │ on each request                               │             │
│           ▼                                               ▼             │
│  ┌─────────────────┐                         ┌───────────────────────┐  │
│  │   query-api     │                         │        grafana        │  │
│  │  Python :8090   │                         │  grafana/grafana      │  │
│  │  /query         │                         │  :3000 (→ host 3001)  │  │
│  │  /metrics/names │                         │  dashboards           │  │
│  │  /metrics proxy │                         │  alert state viewer   │  │
│  └─────────────────┘                         └───────────────────────┘  │
│                                                                          │
│  ┌─────────────────┐   PromQL poll (5 s)                                │
│  │    go-worker    │──────────────────────────────────────────────►      │
│  │  Go binary      │                                                     │
│  │  logs stdout    │                                                     │
│  └─────────────────┘                                                     │
│                                                                          │
│  ┌─────────────────┐                                                     │
│  │    frontend     │                                                     │
│  │  nginx :80      │                                                     │
│  │  dashboard.html │                                                     │
│  └─────────────────┘                                                     │
└──────────────────────────────────────────────────────────────────────────┘
       │            │            │            │
  host:8080    host:8090    host:9090    host:3001
 (raw metrics) (query API) (prometheus)  (grafana)
                                         host:3000
                                        (dashboard)
```

---

## Repository Layout

```
metrics_server/
│
├── server.py                   # metrics-exporter: live UPF traffic simulator
├── metrics.txt                 # baseline counter values used on first boot
│
├── query_api.py                # query-api: filterable JSON layer over the exporter
│
├── main.go                     # go-worker: polls Prometheus with PromQL every 5 s
├── go.mod
├── go.sum
│
├── dashboard.html              # Browser dashboard (single HTML file, no build step)
│
├── prometheus.yml              # Prometheus scrape + alertmanager config
├── rules.yml                   # Recording rules and alert rules
│
├── grafana/
│   └── provisioning/
│       ├── datasources/
│       │   └── prometheus.yml  # Auto-wires Prometheus as the default datasource
│       └── dashboards/
│           ├── provider.yml    # Tells Grafana where to load dashboards from
│           └── upf_metrics.json # UPF Port Bytes dashboard definition
│
├── Dockerfile                  # Image for metrics-exporter
├── Dockerfile.api              # Image for query-api
├── Dockerfile.worker           # Multi-stage image for go-worker
├── Dockerfile.frontend         # Image for frontend (nginx)
│
└── Docker-Compose.yml          # Orchestrates all 6 services
```

---

## Services

### 1. metrics-exporter — port 8080

**File:** `server.py`  
**Image:** built from `Dockerfile`

A Python HTTP server that simulates a live UPF data plane. Rather than serving a static file, it maintains in-memory counters that increment continuously and alternates between a **normal** phase and a **spike** phase on a 30-second cycle (see [Traffic Simulation](#traffic-simulation)).

```
GET /metrics  →  200 text/plain  (Prometheus exposition format)
```

The following UPF-specific metric families are exposed:

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `port_bytes_count` | counter | `dir`, `iface` | Bytes received/transmitted per DPDK port and direction |
| `port_packets_count` | counter | `dir`, `iface` | Packets received/transmitted per DPDK port and direction |
| `port_dropped_count` | counter | `dir`, `iface` | Packets dropped per DPDK port and direction |

Label values: `dir` ∈ `{rx, tx}`, `iface` ∈ `{N3, N6}`.

---

### 2. prometheus — port 9090

**Image:** `prom/prometheus`  
**Config:** `prometheus.yml`, `rules.yml`

Prometheus scrapes `http://metrics-exporter:8080/metrics` every **5 seconds**, evaluates recording and alert rules on the same interval, and stores results in its built-in TSDB.

`prometheus.yml`:
```yaml
global:
  scrape_interval: 5s
  evaluation_interval: 5s

scrape_configs:
  - job_name: "metrics-exporter"
    static_configs:
      - targets: ["metrics-exporter:8080"]

rule_files:
  - "/etc/prometheus/rules.yml"
```

The scrape interval is set to 5 s (rather than the default 15 s) so the `[15s]` rate window in the recording rules contains at least 3 data points and the alert cycle is visible in near-real-time.

The `--web.enable-lifecycle` flag is passed via the Compose `command` block, enabling hot config reloads without restarting the container:

```bash
curl -X POST http://localhost:9090/-/reload
```

Access the Prometheus UI at **http://localhost:9090**.

---

### 3. grafana — port 3001

**Image:** `grafana/grafana`  
**Provisioning:** `grafana/provisioning/`

Grafana is pre-provisioned on startup — no manual datasource or dashboard setup is needed. Anonymous access is enabled with Admin role so there is no login screen.

**Dashboard: UPF Port Bytes** — visible at **http://localhost:3001** under Dashboards.

| Panel | What it shows |
|-------|---------------|
| Uplink Throughput | `rx N3` and `tx N6` byte rates over time |
| Downlink Throughput | `rx N6` and `tx N3` byte rates over time |
| Bytes Dropped / s | Uplink and downlink drop rates with threshold lines at 0.5 MB/s and 1 MB/s |
| Active Alerts | Live table of `ALERTS` from Prometheus; `firing` rows appear red, `pending` orange |

**Alert rules** — visible under **Alerting → Alert rules** in the Grafana sidebar. These are read from Prometheus (the datasource) and are defined in `rules.yml`.

---

### 4. query-api — port 8090

**File:** `query_api.py`  
**Image:** built from `Dockerfile.api`

A Python HTTP server that fetches raw metrics from `metrics-exporter` on demand, parses the Prometheus exposition format, and returns filtered JSON. This is the layer the browser dashboard and any external tooling should call.

**Why a separate service?** `server.py` has no CORS headers. The query-api adds `Access-Control-Allow-Origin: *`, allowing the browser dashboard to call it.

**Configuration** (environment variables):

| Variable | Default | Description |
|----------|---------|-------------|
| `METRICS_SOURCE` | `http://localhost:8080/metrics` | URL to pull raw metrics from |
| `PORT` | `8090` | Port this service listens on |

In Docker Compose, `METRICS_SOURCE` is set to `http://metrics-exporter:8080/metrics`.

See [Query API Reference](#query-api-reference) for full endpoint documentation.

---

### 5. go-worker

**File:** `main.go`  
**Image:** built from `Dockerfile.worker` (multi-stage)

A Go program that connects to Prometheus and repeatedly runs a fixed set of PromQL queries every 5 seconds, logging results to stdout.

**Built-in queries:**

| Label | PromQL |
|-------|--------|
| Port Bytes Count | `port_bytes_count` |
| Port Dropped Count | `port_dropped_count` |
| Port Packets Count | `port_packets_count` |
| PFCP Messages Total | `pfcp_messages_total` |

**Flags:**

| Flag | Default | Description |
|------|---------|-------------|
| `-config <url>` | `http://localhost:9090` | Prometheus base URL |
| `-query <promql>` | *(empty)* | Extra PromQL expression run each cycle |

See [Go Worker Flags](#go-worker-flags) for usage examples.

---

### 6. frontend — port 3000

**File:** `dashboard.html`  
**Image:** built from `Dockerfile.frontend` (nginx:alpine)

A single self-contained HTML file served by Nginx. Fetches metrics from `query-api` entirely in the browser — no build step.

Access at **http://localhost:3000**.

See [Dashboard Guide](#dashboard-guide) for usage.

---

## Recording Rules & Alerts

Defined in `rules.yml`, evaluated every 5 seconds by Prometheus.

### Recording rules

All base rules use `rate(counter[15s])` wrapped in `sum without(dir, iface)`. Stripping the `dir` and `iface` labels from each series is essential: without it, the uplink and downlink series have different label sets and the drop subtraction produces no output.

| Recorded metric | Expression |
|-----------------|-----------|
| `upf:port_bytes_rx_n3:rate5m` | `sum without(dir,iface)(rate(port_bytes_count{dir="rx",iface="N3"}[15s]))` |
| `upf:port_bytes_tx_n6:rate5m` | `sum without(dir,iface)(rate(port_bytes_count{dir="tx",iface="N6"}[15s]))` |
| `upf:port_bytes_rx_n6:rate5m` | `sum without(dir,iface)(rate(port_bytes_count{dir="rx",iface="N6"}[15s]))` |
| `upf:port_bytes_tx_n3:rate5m` | `sum without(dir,iface)(rate(port_bytes_count{dir="tx",iface="N3"}[15s]))` |
| `upf:bytes_dropped_uplink:rate5m` | `upf:port_bytes_rx_n3:rate5m - upf:port_bytes_tx_n6:rate5m` |
| `upf:bytes_dropped_downlink:rate5m` | `upf:port_bytes_rx_n6:rate5m - upf:port_bytes_tx_n3:rate5m` |

### Alert rules

| Alert | Condition | For | Severity |
|-------|-----------|-----|----------|
| `HighBytesDroppedUplink` | `upf:bytes_dropped_uplink:rate5m > 524288` (0.5 MB/s) | 10 s | warning |
| `HighBytesDroppedDownlink` | `upf:bytes_dropped_downlink:rate5m > 524288` (0.5 MB/s) | 10 s | warning |

The `for: 10s` duration means the condition must be continuously true for 10 seconds before the alert transitions from `pending` to `firing`.

---

## Traffic Simulation

`server.py` runs an internal background thread that increments counters every 5 seconds on a **30-second repeating cycle**:

| Phase | Duration | Drop rate | Alert state |
|-------|----------|-----------|-------------|
| Normal | 0 – 15 s | ~10 KB/s (~0.1% loss) | inactive |
| Spike | 15 – 30 s | ~3 MB/s (30% loss) | pending → firing |

During the spike phase, `tx_N6` and `tx_N3` receive only 70% of the bytes that `rx_N3` and `rx_N6` take in respectively. The resulting drop rate (~3 MB/s) is well above the 0.5 MB/s alert threshold.

With a `[15s]` rate window and 5 s scrape interval, each phase is exactly one window long. The rate metric sees only the current phase's data, giving clean on/off transitions in both the Grafana graphs and the alert state.

**Observed timeline per cycle:**

```
t=0  s   Normal starts  → drop ~10 KB/s   → alert: inactive
t=15 s   Spike starts   → drop ~3 MB/s    → alert: pending
t=25 s   for:10s met    → drop ~3 MB/s    → alert: firing
t=30 s   Normal starts  → drop ~10 KB/s   → alert: inactive
```

---

## Quick Start

**Prerequisites:** Docker Desktop (or Docker Engine + Compose plugin).

```bash
cd metrics_server

# Build and start all 6 containers
docker compose up --build -d
```

| Service | URL |
|---------|-----|
| Grafana dashboards & alerts | http://localhost:3001 |
| Prometheus UI | http://localhost:9090 |
| Browser dashboard | http://localhost:3000 |
| Query API | http://localhost:8090 |
| Raw metrics | http://localhost:8080/metrics |
| Worker logs | `docker compose logs -f go-worker` |

**Stop everything:**
```bash
docker compose down
```

**Rebuild a single service after a code change:**
```bash
docker compose up --build metrics-exporter
```

**Hot-reload Prometheus rules/config without restart:**
```bash
curl -X POST http://localhost:9090/-/reload
```

---

## Query API Reference

Base URL: `http://localhost:8090`

All responses are `application/json`. All endpoints include `Access-Control-Allow-Origin: *`.

---

### `GET /metrics/names`

Returns every metric family discovered in the current metrics payload.

**Response:**
```json
{
  "count": 3,
  "metrics": [
    { "name": "port_bytes_count",   "type": "counter", "help": "Bytes received/transmitted by the UPF DPDK port" },
    { "name": "port_packets_count", "type": "counter", "help": "Packets received/transmitted by the UPF DPDK port" },
    { "name": "port_dropped_count", "type": "counter", "help": "Packets dropped on the UPF DPDK port" }
  ]
}
```

---

### `GET /query`

Returns all series for a metric family, with optional label filtering.

**Query parameters:**

| Parameter | Required | Description |
|-----------|----------|-------------|
| `metric` | Yes | Metric family name (case-insensitive). |
| *any label name* | No | Filter by label value. Multiple filters are AND-combined. |

**Examples:**

```bash
# All series
curl "http://localhost:8090/query?metric=port_bytes_count"

# Filter by direction
curl "http://localhost:8090/query?metric=port_bytes_count&dir=rx"

# Filter by direction and interface
curl "http://localhost:8090/query?metric=port_bytes_count&dir=rx&iface=N3"
```

**Error responses:**

| HTTP code | Cause |
|-----------|-------|
| `400` | `metric` parameter missing |
| `404` | Metric name not found |
| `502` | Could not reach `METRICS_SOURCE` |

---

### `GET /metrics`

Proxies the raw Prometheus exposition text from `METRICS_SOURCE` with a CORS header.

```bash
curl http://localhost:8090/metrics
```

---

## Dashboard Guide

Open **http://localhost:3000** after running `docker compose up`.

### Controls bar

| Control | Purpose |
|---------|---------|
| **ENDPOINT** field | URL to fetch metrics from. Default: `http://localhost:8090/metrics`. |
| **FETCH** button | Trigger an immediate refresh. |
| **FILTER** field | Show only matching metric families. Updates live as you type. |
| **Auto-refresh 5s** checkbox | Toggle automatic polling. |

### Metric panels

Each Prometheus metric family gets its own collapsible panel. The coloured badge shows the metric type:

| Colour | Type |
|--------|------|
| Cyan | `gauge` |
| Orange | `counter` |
| Purple | `histogram` |
| Amber | `summary` |

Values use human-readable suffixes (`K`, `M`, `G`). Changed values flash green between refreshes.

---

## Go Worker Flags

### `-config <url>`

Sets the Prometheus base URL.

```bash
docker compose run --rm go-worker -config http://prometheus:9090
```

### `-query <promql>`

Appends a custom PromQL expression to every poll cycle.

```bash
docker compose run --rm go-worker \
  -config http://prometheus:9090 \
  -query 'upf:bytes_dropped_uplink:rate5m'
```

### Viewing output

```bash
docker compose logs -f go-worker
docker compose logs --tail=50 go-worker
```

---

## Configuration Reference

### Prometheus scrape & evaluation interval

`prometheus.yml`:
```yaml
global:
  scrape_interval: 5s
  evaluation_interval: 5s
```

Set to 5 s to keep the `[15s]` rate window populated with at least 3 data points. Increasing this will slow alert transitions.

### Alert thresholds

`rules.yml` — edit the `expr` field on the alert rules:
```yaml
expr: (upf:bytes_dropped_uplink:rate5m) > 524288   # 0.5 MB/s
```

### Simulator cycle

`server.py` — top-level constants:
```python
CYCLE       = 30   # total cycle length in seconds
SPIKE_START = 15   # spike begins this many seconds into the cycle
```

### query-api environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `METRICS_SOURCE` | `http://localhost:8080/metrics` | Metrics endpoint to pull from |
| `PORT` | `8090` | Port the query-api listens on |

---

## Port Map

| Host port | Container port | Service |
|-----------|---------------|---------|
| `3000` | `80` | frontend (nginx) |
| `3001` | `3000` | grafana |
| `8080` | `8080` | metrics-exporter |
| `8090` | `8090` | query-api |
| `9090` | `9090` | prometheus |

`go-worker` exposes no ports — it only writes to stdout.

---

## Running Locally Without Docker

### metrics-exporter
```bash
python3 server.py
# Listening on http://localhost:8080
```

### query-api
```bash
METRICS_SOURCE=http://localhost:8080/metrics PORT=8090 python3 query_api.py
```

### go-worker
```bash
go run main.go -config http://localhost:9090
```

### prometheus
```bash
prometheus --config.file=prometheus.yml --web.enable-lifecycle
```

### grafana
Point a local Grafana instance at `http://localhost:9090` as a Prometheus datasource, then import `grafana/provisioning/dashboards/upf_metrics.json`.

### frontend
Open `dashboard.html` directly in a browser, or serve it locally:
```bash
python3 -m http.server 3000
# Visit http://localhost:3000/dashboard.html
```

---

## How the Prometheus Parser Works

Both `query_api.py` and `dashboard.html` parse the [Prometheus exposition format](https://prometheus.io/docs/instrumenting/exposition_formats/).

### Format overview

```
# HELP metric_name Human-readable description.
# TYPE metric_name counter
metric_name{label1="val1",label2="val2"} 123.45
```

### Label filtering logic

```
metric=port_bytes_count                      → all 4 series
metric=port_bytes_count&dir=rx               → 2 series  (N3 + N6, rx only)
metric=port_bytes_count&dir=rx&iface=N3      → 1 series
```

Filters are AND-combined. A series is included only if every supplied label filter matches. Missing labels count as non-matching.
