# URL Shortener Metrics Stack

A containerised observability stack built around a real Go URL shortener. The shortener generates genuine Prometheus metrics (request rate, latency histograms, analytics queue depth) that are scraped, stored, visualised, and queryable through several complementary interfaces.

---

## Architecture

```
Browser
  ├── :3002/            → Shortener UI  (Nginx, proxies /api/* → shortener)
  ├── :3002/metrics.html → Metrics Explorer (live Prometheus text viewer)
  ├── :3001/            → Grafana dashboards
  └── :8090/            → Query API (filterable JSON over /metrics)

shortener (:8080)       Go binary — HTTP API + /metrics
prometheus (:9090)      Scrapes shortener every 5 s, evaluates alert rules
grafana (:3001)         Dashboards provisioned from grafana/provisioning/
query-api (:8090)       Python — parses /metrics text, exposes JSON endpoints
go-worker               Go binary — polls Prometheus with PromQL every 5 s
frontend (:3002)        Nginx — serves UI, proxies /api/ to shortener
```

---

## Quick Start

```bash
docker compose up --build
```

| URL | What you get |
|-----|-------------|
| http://localhost:3002/ | Shortener UI — paste a URL, get a short code |
| http://localhost:3002/metrics.html | Live Prometheus text explorer |
| http://localhost:3001/ | Grafana — URL Shortener dashboard |
| http://localhost:9090/ | Prometheus UI |
| http://localhost:8090/metrics/names | All metric families (JSON) |
| http://localhost:8090/query?metric=http_requests_total | Filtered metric query |

---

## Services

### shortener — port 8080

Go binary in `cmd/shortener/`. Library code in `shortener/`.

**Endpoints:**
- `POST /shorten` — body `{"url":"..."}` → `{"code":"abc123","short_url":"..."}`
- `GET /{code}` — 302 redirect to original URL; enqueues analytics task
- `GET /stats/{code}` — `{"code":"...","clicks":42}`
- `GET /metrics` — Prometheus text format

**Metrics emitted:**

| Metric | Type | Description |
|--------|------|-------------|
| `http_requests_total` | CounterVec | Requests by method, path, status |
| `http_request_duration_seconds` | HistogramVec | Latency by method, path |
| `shortener_urls_created_total` | Counter | Total URLs shortened |
| `shortener_redirects_total` | CounterVec | Redirects per short code |
| `analytics_queue_depth` | Gauge | Tasks waiting in the channel |
| `analytics_tasks_processed_total` | CounterVec | Tasks ok vs. dropped |
| `analytics_task_duration_seconds` | Histogram | Worker processing time |

**Flags:** `-addr :8080` `-workers 3` `-queue 100`

---

### query-api — port 8090

Python service (`query_api.py`). Fetches raw Prometheus text from the shortener and exposes a filterable JSON API. Set `METRICS_SOURCE` env var to point at a different exporter.

**Endpoints:**
- `GET /metrics/names` — list all metric families
- `GET /query?metric=<name>[&label=value…]` — filtered series
- `GET /metrics` — raw Prometheus text proxy (with CORS header)

**Examples:**
```bash
curl http://localhost:8090/metrics/names
curl "http://localhost:8090/query?metric=http_requests_total&status=200"
curl "http://localhost:8090/query?metric=http_request_duration_seconds&suffix=bucket"
```

---

### go-worker

Go binary (`main.go`). Connects to Prometheus and polls a set of PromQL queries every 5 seconds, logging results. Useful for scripted monitoring or sanity checks.

**Flags:** `-config <prometheus-url>` `-query <extra-promql>`

---

### prometheus — port 9090

Scrapes `shortener:8080` every 5 s. Alert rules in `rules.yml`:

| Alert | Condition |
|-------|-----------|
| `HighErrorRate` | >5% of requests return 5xx for 10 s |
| `QueueNearCapacity` | Queue depth >80 for 10 s |
| `TasksBeingDropped` | Drop rate >0 for 30 s |

---

### grafana — port 3001

Dashboard provisioned from `grafana/provisioning/dashboards/url_shortener.json`. No login required (anonymous admin).

Panels: HTTP Request Rate · Latency p50/p95/p99 · URLs Created · Queue Depth · Tasks ok/dropped

---

### frontend — port 3002

Nginx serving two pages and proxying the shortener API:

- `/` → `shortener_app.html` — URL shortener UI
- `/metrics.html` → `dashboard.html` — Prometheus metrics explorer
- `/api/*` → proxied to `shortener:8080/*`

---

## Repository Layout

```
├── docker-compose.yml          wires all six services
├── .dockerignore
├── go.mod / go.sum
│
├── cmd/
│   ├── shortener/
│   │   └── main.go             shortener binary entrypoint (flags, wiring)
│   └── worker/
│       └── main.go             go-worker binary
│
├── shortener/                  Go library package
│   ├── metrics.go              Prometheus metric definitions
│   ├── store.go                in-memory URL store (sync.Map + atomic)
│   ├── worker.go               buffered channel queue + worker pool
│   ├── server.go               HTTP handlers + instrumentation middleware
│   └── *_test.go               unit tests (one per file)
│
├── frontend/
│   ├── Dockerfile
│   ├── index.html              URL shortener UI
│   ├── metrics.html            Prometheus text explorer
│   └── nginx.conf              proxy /api/* → shortener:8080
│
├── query-api/
│   ├── Dockerfile
│   └── query_api.py            filterable JSON API over /metrics
│
├── docker/
│   ├── Dockerfile.shortener    multi-stage Go build for shortener
│   └── Dockerfile.worker       multi-stage Go build for worker
│
└── infra/
    ├── prometheus.yml          scrape config (shortener:8080, 5 s interval)
    ├── rules.yml               recording rules + alert rules
    └── grafana/
        └── provisioning/
            ├── dashboards/
            │   ├── provider.yml
            │   └── url_shortener.json
            └── datasources/
                └── prometheus.yml
```
