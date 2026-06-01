# UPF Metrics Stack

A containerised observability stack for a 5G User Plane Function (UPF). It collects Prometheus-format metrics, stores them in Prometheus, exposes a filterable JSON query API, runs a Go polling worker, and serves a live browser dashboard — all as separate Docker containers.

---

## Table of Contents

1. [Architecture](#architecture)
2. [Repository Layout](#repository-layout)
3. [Services](#services)
   - [metrics-exporter](#1-metrics-exporter--port-8080)
   - [prometheus](#2-prometheus--port-9090)
   - [query-api](#3-query-api--port-8090)
   - [go-worker](#4-go-worker)
   - [frontend](#5-frontend--port-3000)
4. [Quick Start](#quick-start)
5. [Query API Reference](#query-api-reference)
6. [Dashboard Guide](#dashboard-guide)
7. [Go Worker Flags](#go-worker-flags)
8. [Configuration Reference](#configuration-reference)
9. [Port Map](#port-map)
10. [Running Locally Without Docker](#running-locally-without-docker)
11. [How the Prometheus Parser Works](#how-the-prometheus-parser-works)

---

## Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│                        Docker network                              │
│                                                                    │
│  ┌─────────────────┐   scrape /metrics    ┌──────────────────┐    │
│  │ metrics-exporter│◄─────────────────────│   prometheus     │    │
│  │  Python :8080   │    every 15 s        │  prom/prometheus  │    │
│  │  serves         │                      │  :9090           │    │
│  │  metrics.txt    │                      └─────────┬────────┘    │
│  └────────┬────────┘                               │              │
│           │ fetch /metrics                  PromQL │query         │
│           │ on each request                        │              │
│           ▼                                        ▼              │
│  ┌─────────────────┐                    ┌──────────────────┐      │
│  │   query-api     │                    │    go-worker     │      │
│  │  Python :8090   │                    │  Go binary       │      │
│  │  /query         │                    │  polls every 5 s │      │
│  │  /metrics/names │                    │  logs to stdout  │      │
│  │  /metrics proxy │                    └──────────────────┘      │
│  └─────────────────┘                                              │
│                                                                    │
│  ┌─────────────────┐                                              │
│  │    frontend     │                                              │
│  │  nginx :80      │                                              │
│  │  dashboard.html │                                              │
│  └─────────────────┘                                              │
└────────────────────────────────────────────────────────────────────┘
         │                    │                    │
    localhost:8080       localhost:8090       localhost:3000
    (raw metrics)        (query API)          (dashboard)
         │                    ▲
         └────────────────────┘
              browser fetches
              via query-api
              (CORS enabled)
```

**Key design decisions:**

- `metrics-exporter` (`server.py`) is treated as **read-only** — it simulates the real UPF application and is never modified.
- The `query-api` acts as a CORS-enabled intermediary: the browser cannot call `metrics-exporter` directly (no CORS header there), so it goes through `query-api` which adds `Access-Control-Allow-Origin: *`.
- `prometheus` and `go-worker` communicate only inside the Docker network; neither needs a public port for the dashboard to work.

---

## Repository Layout

```
metrics_server/
│
├── server.py               # metrics-exporter: serves metrics.txt at /metrics
├── metrics.txt             # Prometheus exposition format data (UPF metrics)
│
├── query_api.py            # query-api: filterable JSON layer over the exporter
│
├── main.go                 # go-worker: polls Prometheus with PromQL every 5 s
├── go.mod                  # Go module manifest
├── go.sum                  # Go dependency checksums
│
├── dashboard.html          # Browser dashboard (single HTML file, no build step)
│
├── prometheus.yml          # Prometheus scrape configuration
│
├── Dockerfile              # Image for metrics-exporter
├── Dockerfile.api          # Image for query-api
├── Dockerfile.worker       # Multi-stage image for go-worker
├── Dockerfile.frontend     # Image for frontend (nginx)
│
└── Docker-Compose.yml      # Orchestrates all 5 services
```

---

## Services

### 1. metrics-exporter — port 8080

**File:** `server.py`  
**Image:** built from `Dockerfile`

A minimal Python HTTP server that reads `metrics.txt` and serves it verbatim at `GET /metrics`. This simulates the `/metrics` endpoint that a real UPF application would expose.

```
GET /metrics  →  200 text/plain  (Prometheus exposition format)
```

> **Do not modify `server.py`.** In production this will be replaced by the real application. Treat it as a black box.

`metrics.txt` contains the following metric families (among Go runtime internals):

| Metric | Type | Description |
|--------|------|-------------|
| `port_bytes_count` | counter | Bytes received/sent per DPDK port and direction |
| `port_packets_count` | counter | Packets received/sent per DPDK port and direction |
| `port_dropped_count` | counter | Packets dropped per DPDK port and direction |
| `pfcp_messages_total` | counter | PFCP messages by direction, type, and result |
| `pfcp_messages_duration_seconds` | histogram | PFCP message latency (bucket/sum/count) |
| `pfcp_sessions_total` | gauge | Active PFCP sessions in the UPF |
| `datapath_down` | gauge | Datapath failure counts by reason |
| `upf_latency_ns` | summary | Packet processing latency percentiles per interface |
| `upf_jitter_ns` | summary | Packet processing jitter percentiles per interface |
| `upf_total_bytes` | counter | Total bytes processed by the UPF per interface and slice |
| `upf_total_packets` | counter | Total packets processed by the UPF per interface and slice |

---

### 2. prometheus — port 9090

**Image:** `prom/prometheus` (official)  
**Config:** `prometheus.yml`

Prometheus scrapes `http://metrics-exporter:8080/metrics` every 15 seconds and stores the data in its built-in time-series database (TSDB). This enables historical queries, graphing, and alerting.

`prometheus.yml`:
```yaml
global:
  scrape_interval: 15s

scrape_configs:
  - job_name: "metrics-exporter"
    static_configs:
      - targets: ["metrics-exporter:8080"]
```

The hostname `metrics-exporter` resolves to the exporter container because Docker Compose puts all services on the same internal network and assigns each service its own DNS name.

Access the Prometheus UI at **http://localhost:9090** to run PromQL queries manually.

---

### 3. query-api — port 8090

**File:** `query_api.py`  
**Image:** built from `Dockerfile.api`

A Python HTTP server that fetches the raw metrics from `metrics-exporter` on demand, parses the Prometheus exposition format, and returns filtered JSON. This is the layer the browser dashboard and any external tooling should call.

Why a separate service instead of adding endpoints to `server.py`?  
Because `server.py` is read-only. The query-api also adds `Access-Control-Allow-Origin: *`, which `server.py` does not have, allowing the browser dashboard to call it.

**Configuration** (environment variables):

| Variable | Default | Description |
|----------|---------|-------------|
| `METRICS_SOURCE` | `http://localhost:8080/metrics` | URL to pull raw metrics from |
| `PORT` | `8090` | Port this service listens on |

In Docker Compose `METRICS_SOURCE` is set to `http://metrics-exporter:8080/metrics` (internal Docker hostname).

See [Query API Reference](#query-api-reference) for full endpoint documentation.

---

### 4. go-worker

**File:** `main.go`  
**Image:** built from `Dockerfile.worker` (multi-stage)

A Go program that connects to Prometheus and repeatedly runs a fixed set of PromQL queries every 5 seconds, logging the results to stdout (visible via `docker compose logs go-worker`).

**Built-in queries** (always run):

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
| `-query <promql>` | *(empty)* | Extra PromQL expression appended to each poll cycle |

See [Go Worker Flags](#go-worker-flags) for usage examples.

**Build approach — multi-stage Dockerfile:**

```dockerfile
# Stage 1: compile (golang:alpine image, ~400 MB)
FROM golang:alpine AS builder
ENV GOTOOLCHAIN=local
WORKDIR /build
COPY go.mod go.sum ./
RUN go mod download        # cached layer; only re-runs when go.mod changes
COPY main.go .
RUN go build -o worker .

# Stage 2: run (alpine:3.20, ~10 MB — no Go toolchain)
FROM alpine:3.20
COPY --from=builder /build/worker .
ENTRYPOINT ["./worker"]
CMD ["-config", "http://prometheus:9090"]
```

`GOTOOLCHAIN=local` prevents Go from attempting to download version `1.26.3` declared in `go.mod` (that version is not yet available as a Docker image); it forces the build to use whatever Go version is in the base image.

---

### 5. frontend — port 3000

**File:** `dashboard.html`  
**Image:** built from `Dockerfile.frontend` (nginx:alpine)

A single self-contained HTML file served by Nginx. It fetches metrics from the `query-api` (port 8090) entirely in the browser — no build step, no Node.js, no bundler.

Access at **http://localhost:3000**.

See [Dashboard Guide](#dashboard-guide) for usage.

---

## Quick Start

**Prerequisites:** Docker Desktop (or Docker Engine + Compose plugin).

```bash
# Clone / navigate to the project directory
cd metrics_server

# Build and start all 5 containers
docker compose up --build

# To run in the background
docker compose up --build -d
```

| Service | URL |
|---------|-----|
| Dashboard | http://localhost:3000 |
| Query API | http://localhost:8090 |
| Raw metrics | http://localhost:8080/metrics |
| Prometheus UI | http://localhost:9090 |
| Worker logs | `docker compose logs -f go-worker` |

**Stop everything:**
```bash
docker compose down
```

**Rebuild a single service after a code change:**
```bash
docker compose up --build query-api
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
  "count": 51,
  "metrics": [
    {
      "name": "datapath_down",
      "type": "gauge",
      "help": "Reason for datapath failure"
    },
    {
      "name": "pfcp_messages_total",
      "type": "counter",
      "help": "Counter for incoming and outgoing PFCP messages"
    },
    {
      "name": "port_bytes_count",
      "type": "counter",
      "help": "Shows the number of bytes received by the UPF DPDK port"
    }
  ]
}
```

Use this endpoint first to discover what metric names are available before calling `/query`.

---

### `GET /query`

Returns all series for a metric family, with optional label filtering.

**Query parameters:**

| Parameter | Required | Description |
|-----------|----------|-------------|
| `metric` | Yes | Metric family name. Case-insensitive (`PORT_BYTES_COUNT` = `port_bytes_count`). |
| `suffix` | No | For histograms/summaries: filter to one sub-type (`bucket`, `sum`, or `count`). |
| *any label name* | No | Filter by label value. Multiple label filters are AND-combined. Values are case-sensitive. |

**Response fields:**

| Field | Description |
|-------|-------------|
| `metric` | Canonical metric family name |
| `type` | Prometheus type: `counter`, `gauge`, `histogram`, `summary` |
| `help` | Description from `# HELP` comment |
| `filters` | Echo of applied filters |
| `count` | Number of series returned |
| `results` | Array of `{labels, value[, suffix]}` objects |

---

**Examples — progressive filtering:**

#### All series (no filter)
```bash
curl "http://localhost:8090/query?metric=port_bytes_count"
```
```json
{
  "metric": "port_bytes_count",
  "type": "counter",
  "help": "Shows the number of bytes received by the UPF DPDK port",
  "filters": {},
  "count": 4,
  "results": [
    { "labels": { "dir": "rx", "iface": "N3" }, "value": "817404" },
    { "labels": { "dir": "rx", "iface": "N6" }, "value": "817464" },
    { "labels": { "dir": "tx", "iface": "N3" }, "value": "842870" },
    { "labels": { "dir": "tx", "iface": "N6" }, "value": "842716" }
  ]
}
```

#### Filter by one label
```bash
curl "http://localhost:8090/query?metric=port_bytes_count&dir=rx"
```
```json
{
  "filters": { "dir": "rx" },
  "count": 2,
  "results": [
    { "labels": { "dir": "rx", "iface": "N3" }, "value": "817404" },
    { "labels": { "dir": "rx", "iface": "N6" }, "value": "817464" }
  ]
}
```

#### Filter by two labels
```bash
curl "http://localhost:8090/query?metric=port_bytes_count&dir=rx&iface=N3"
```
```json
{
  "filters": { "dir": "rx", "iface": "N3" },
  "count": 1,
  "results": [
    { "labels": { "dir": "rx", "iface": "N3" }, "value": "817404" }
  ]
}
```

#### Histogram — filter to one sub-type
```bash
# All sub-types (bucket, sum, count)
curl "http://localhost:8090/query?metric=pfcp_messages_duration_seconds"

# Only the bucket series
curl "http://localhost:8090/query?metric=pfcp_messages_duration_seconds&suffix=bucket"

# One specific bucket (le = upper bound in seconds)
curl "http://localhost:8090/query?metric=pfcp_messages_duration_seconds&suffix=bucket&le=0.001"
```

#### Summary — latency percentiles for one interface
```bash
curl "http://localhost:8090/query?metric=upf_latency_ns&iface=N3"
```

#### Case-insensitive metric name
```bash
# All equivalent:
curl "http://localhost:8090/query?metric=PORT_BYTES_COUNT"
curl "http://localhost:8090/query?metric=Port_Bytes_Count"
curl "http://localhost:8090/query?metric=port_bytes_count"
```

---

### `GET /metrics`

Proxies the raw Prometheus exposition text from `METRICS_SOURCE`, adding a CORS header. Identical in content to `http://localhost:8080/metrics`.

```bash
curl http://localhost:8090/metrics
```

The dashboard uses this endpoint instead of calling port 8080 directly (which lacks CORS).

---

### Error responses

| HTTP code | Cause |
|-----------|-------|
| `400` | `metric` parameter missing |
| `404` | Metric name not found (response includes `available` list) |
| `502` | Could not reach `METRICS_SOURCE` |

---

## Dashboard Guide

Open **http://localhost:3000** after running `docker compose up`.

### Controls bar

| Control | Purpose |
|---------|---------|
| **ENDPOINT** field | URL to fetch metrics from. Default: `http://localhost:8090/metrics`. Change to any Prometheus-format endpoint and click **FETCH**. |
| **FETCH** button | Manually trigger an immediate refresh. |
| **FILTER** field | Type any substring to show only matching metric families (`port`, `pfcp`, `upf`, `go_gc`, …). Updates live as you type. |
| **Auto-refresh 5s** checkbox | Toggle automatic polling. A progress bar below the header shows time until the next fetch. |

### Stats strip

The strip below the header shows live values for the most important UPF metrics: port byte counts, dropped packets, PFCP session count, PFCP message total, and datapath error count. These update on every refresh.

### Metric panels

Each Prometheus metric family gets its own collapsible panel.

- Click the panel header to collapse or expand it.
- The coloured badge on the left shows the metric type:

  | Colour | Type |
  |--------|------|
  | Cyan | `gauge` |
  | Orange | `counter` |
  | Purple | `histogram` |
  | Amber | `summary` |

- Each column in the table corresponds to a Prometheus label key.
- The **VALUE** column uses human-readable suffixes: `K`, `M`, `G`, `T`.
- When a value changes between refreshes, it flashes green.
- UPF-specific metrics (`port_*`, `pfcp_*`, `upf_*`, `datapath_down`) are sorted to the top. Go runtime and process metrics appear after.

### Pointing the dashboard at a different source

1. Edit the **ENDPOINT** field in the controls bar.
2. Click **FETCH**.

The dashboard accepts any URL that serves Prometheus exposition format text.

---

## Go Worker Flags

The `go-worker` container accepts two command-line flags.

### `-config <url>`

Sets the Prometheus base URL. Default: `http://localhost:9090`.

```bash
# Use the default (prometheus container in Compose network)
docker compose up go-worker

# Point at an external Prometheus
docker compose run --rm go-worker -config http://192.168.1.100:9090

# Point at a different internal service (if you rename prometheus)
docker compose run --rm go-worker -config http://my-prom:9090
```

To make a change permanent, edit the `command` field in `Docker-Compose.yml`:

```yaml
go-worker:
  command: ["-config", "http://my-prom:9090"]
```

### `-query <promql>`

Appends a custom PromQL expression to every poll cycle. Default: empty (no extra query).

```bash
# Watch a specific metric
docker compose run --rm go-worker \
  -config http://prometheus:9090 \
  -query 'upf_total_bytes'

# PromQL with label selector
docker compose run --rm go-worker \
  -config http://prometheus:9090 \
  -query 'port_bytes_count{dir="rx"}'

# PromQL aggregation
docker compose run --rm go-worker \
  -config http://prometheus:9090 \
  -query 'sum(port_packets_count) by (iface)'
```

### Viewing worker output

```bash
# Follow logs live
docker compose logs -f go-worker

# Last 50 lines
docker compose logs --tail=50 go-worker
```

Sample output:
```
2026/06/01 10:32:15 connecting to Prometheus at http://prometheus:9090
2026/06/01 10:32:15 === 10:32:15 ===
2026/06/01 10:32:15 [Port Bytes Count]
2026/06/01 10:32:15   {dir="rx", iface="N3"} => 817404
2026/06/01 10:32:15   {dir="rx", iface="N6"} => 817464
2026/06/01 10:32:15   {dir="tx", iface="N3"} => 842870
2026/06/01 10:32:15   {dir="tx", iface="N6"} => 842716
2026/06/01 10:32:15 [PFCP Messages Total]
2026/06/01 10:32:15   {direction="Outgoing", message_type="Association Setup Request", ...} => 6
```

---

## Configuration Reference

### Environment variables (query-api)

Set in `Docker-Compose.yml` under the `query-api` service's `environment` block.

| Variable | Default | Description |
|----------|---------|-------------|
| `METRICS_SOURCE` | `http://localhost:8080/metrics` | Full URL of the Prometheus `/metrics` endpoint to pull from. Change this to point the query-api at a different exporter. |
| `PORT` | `8090` | TCP port the query-api listens on inside the container. If you change this, update the `ports` mapping in Compose as well. |

Example — point query-api at a remote exporter:
```yaml
query-api:
  environment:
    - METRICS_SOURCE=http://10.0.0.5:9100/metrics
    - PORT=8090
```

### Prometheus scrape interval

Edit `prometheus.yml`:
```yaml
global:
  scrape_interval: 15s   # change to 5s for faster updates
```

---

## Port Map

| Port (host) | Container | Service |
|-------------|-----------|---------|
| `3000` | `80` | frontend (nginx) |
| `8080` | `8080` | metrics-exporter |
| `8090` | `8090` | query-api |
| `9090` | `9090` | prometheus |

The `go-worker` container exposes no ports — it only writes to stdout.

---

## Running Locally Without Docker

All services can be run directly on the host for development.

### metrics-exporter
```bash
# Requires Python 3.x
# metrics.txt must be in the same directory as server.py
# (the server uses the hardcoded path /app/metrics.txt when in Docker;
#  locally you may need to create a symlink or adjust the path)
python3 server.py
# Listening on http://localhost:8080
```

### query-api
```bash
METRICS_SOURCE=http://localhost:8080/metrics PORT=8090 python3 query_api.py
# Listening on http://localhost:8090
```

### go-worker
```bash
# Requires Go 1.24+
go run main.go -config http://localhost:9090
# Or build first:
go build -o worker .
./worker -config http://localhost:9090 -query upf_total_bytes
```

### prometheus
```bash
# Requires Prometheus binary in PATH
prometheus --config.file=prometheus.yml
# UI at http://localhost:9090
```

### frontend
Open `dashboard.html` directly in a browser:
```
file:///path/to/metrics_server/dashboard.html
```
> Note: some browsers block `file://` → `http://` fetch requests. If the dashboard shows a connection error, run the query-api locally and open the dashboard via a local server instead:
> ```bash
> python3 -m http.server 3000
> # Then visit http://localhost:3000/dashboard.html
> ```

---

## How the Prometheus Parser Works

Both `query_api.py` and `dashboard.html` contain a parser for the [Prometheus exposition format](https://prometheus.io/docs/instrumenting/exposition_formats/). Understanding it helps when adding new metrics.

### Format overview

```
# HELP metric_name Human-readable description.
# TYPE metric_name gauge
metric_name{label1="val1",label2="val2"} 123.45
metric_name{label1="val1",label2="val3"} 678.90
```

### Metric families

A **family** is the logical grouping of all series sharing the same base name. Histograms and summaries generate multiple line types that are folded back into one family:

| Line in text | Base family | `suffix` field |
|---|---|---|
| `pfcp_messages_duration_seconds_bucket{le="0.001",...}` | `pfcp_messages_duration_seconds` | `bucket` |
| `pfcp_messages_duration_seconds_sum{...}` | `pfcp_messages_duration_seconds` | `sum` |
| `pfcp_messages_duration_seconds_count{...}` | `pfcp_messages_duration_seconds` | `count` |
| `port_bytes_count{dir="rx",...}` | `port_bytes_count` | *(none)* |

The parser resolves the base family by checking whether the metric name matches a known `# TYPE` entry exactly, or starts with one and ends in a recognised suffix (`bucket`, `sum`, `count`, `created`).

### Label filtering logic

```
metric=port_bytes_count             → all 4 series
metric=port_bytes_count&dir=rx      → series where labels["dir"] == "rx"  (2 results)
metric=port_bytes_count&dir=rx&iface=N3  → labels["dir"]=="rx" AND labels["iface"]=="N3"  (1 result)
```

Filters are AND-combined. A series is included only if **every** supplied label filter matches. Missing labels count as non-matching (`series.labels.get(k) == v` returns `False` if key absent).
