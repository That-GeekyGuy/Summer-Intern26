# Listener Server — TCP vs UDP Throughput Comparison

A self-contained benchmark stack that runs a TCP listener and a UDP listener
side-by-side, measures their throughput ceilings, and visualises everything in
Grafana.

---

## Why UDP is faster than TCP

| Step | TCP | UDP |
|------|-----|-----|
| Generator "send one packet" | `net.DialTimeout("tcp", …)` — full 3-way kernel handshake (~0.1 ms on localhost) | `net.Dial("udp", …)` — local syscall only, no network round-trip (~1–5 µs) |
| Listener "receive one packet" | `Accept()` — blocks until OS completes handshake, then spawns a goroutine | `ReadFromUDP()` — single goroutine reads the next datagram from the kernel buffer |
| Ceiling bottleneck | TCP handshake rate × worker count | Single `ReadFromUDP` goroutine (recv-loop bound) |

**Result**: on a typical developer machine, UDP delivers **10–50× more packets/sec** than TCP at the same worker count.

### Why TCP ceiling grows with `N_WORKERS`

Each pool worker handles one TCP connection at a time.  With `HANDLER_DELAY_MS=0`
a worker takes ~0.1–0.5 ms per connection, so:

```
TCP ceiling ≈ N_WORKERS / handler_time_seconds
```

Doubling workers doubles the ceiling — until the OS TCP stack becomes the limit
(roughly 100 K–500 K connections/s on modern Linux).

### Why UDP ceiling plateaus early

The UDP listener has **one** `ReadFromUDP` goroutine feeding a worker pool.
Adding more pool workers does not speed up the receive loop.  The plateau you
see in the stress-test table is the `ReadFromUDP` goroutine's maximum rate, not
the workers' rate.

---

## Directory layout

```
listener_server/
├── tcp/
│   ├── listener/          Raw TCP server (accept-loop → bounded worker pool)
│   └── generator/         TCP load generator (new conn per packet, async ACK)
├── udp/
│   ├── listener/          UDP server (single ReadFromUDP → bounded worker pool)
│   └── generator/         UDP load generator (new socket per packet, async ACK)
├── monitoring/
│   ├── prometheus/        prometheus.yml  (scrapes both listeners + node-exporter)
│   └── grafana/
│       ├── provisioning/  Auto-wired datasource + dashboard provider
│       └── dashboards/    comparison.json  (19 live panels + stress-test results)
├── stress-metrics/        Written by stress_test.sh, mounted into node-exporter
├── docker-compose.yaml
├── stress_test.sh
├── .env.example
└── results.txt            (created after first stress_test.sh run)
```

---

## Quick start

### 1 — Copy secrets file

```bash
cp .env.example .env          # edit if you want non-default Grafana credentials
```

### 2 — Start the monitoring stack

```bash
mkdir -p stress-metrics       # node-exporter textfile mount (one-time)
docker compose up --build
```

Services:

| Service | URL |
|---------|-----|
| TCP listener | `localhost:8080` (raw TCP) |
| UDP listener | `localhost:8082/udp` (raw UDP) |
| TCP metrics | `http://localhost:2112/metrics` |
| UDP metrics | `http://localhost:2114/metrics` |
| Prometheus | `http://localhost:9090` |
| Grafana | `http://localhost:3000` (admin / admin) |

Open Grafana → **TCP vs UDP — Live Comparison** dashboard.

---

## Sending packets with curl

Both listeners expose a `/run` endpoint on their metrics port.  It triggers a
self-load-test: the listener sends N packets to itself using W parallel workers.

```bash
# Send 1 000 TCP packets with 8 workers
curl "http://localhost:2112/run?count=1000&workers=8"

# Send 10 000 UDP packets with 32 workers
curl "http://localhost:2114/run?count=10000&workers=32"
```

Watch the **Throughput** and **Active Connections / Workers** panels update in
real time.

---

## Running the stress test

```bash
./stress_test.sh
```

Builds all four binaries, then for each listener pool size in `WORKERS_LIST`:

1. Starts the listener on a **separate port** (8081/8083) — no clash with Docker.
2. Fires an unlimited-rate burst generator (`ASYNC_ACK=1`) in the background.
3. Measures actual throughput **from the listener's Prometheus metrics** (not the
   generator's side — the generator never becomes the bottleneck).
4. Doubles generator-worker count until the listener-side rate plateaus (<10%
   improvement) — that plateau is the throughput **ceiling**.
5. Repeats for UDP.

### Options

```bash
./stress_test.sh --workers "1 2 4 8 16"   # listener pool sizes (default)
./stress_test.sh --workers "4 16" --duration 10
PROBE_DURATION=3 STRESS_WORKERS="1 4 16" ./stress_test.sh
```

### Output files

| File | Contents |
|------|----------|
| `results.txt` | Plain-text comparison table (no ANSI codes) |
| `stress-metrics/stress_test.prom` | Prometheus textfile — node-exporter picks this up automatically within one scrape cycle (1 s) |

After the run, the Grafana dashboard's **Stress Test Results** row populates
automatically:

- Best TCP ceiling (stat)
- Best UDP ceiling (stat)
- UDP/TCP speedup factor (stat)
- Last run timestamp (stat)
- Bar chart: ceiling per pool size, TCP vs UDP side-by-side

---

## Environment variables

### Listener (both TCP and UDP)

| Variable | Default | Description |
|----------|---------|-------------|
| `LISTEN_ADDR` | `:8080` / `:8082` | Address to accept connections/datagrams on |
| `METRICS_ADDR` | `:2112` / `:2114` | Prometheus `/metrics` + `/run` endpoint |
| `WORKER_POOL_SIZE` | `0` | Pool workers.  `0` = unbounded (every packet gets its own goroutine) |
| `QUEUE_SIZE` | `WORKER_POOL_SIZE × 2` | Pending-work queue depth (only when pool > 0) |
| `HANDLER_DELAY_MS` | `0` | Artificial delay per packet — simulates real work; set >0 to model CPU-bound handlers |

### Generator

| Variable | Default | Description |
|----------|---------|-------------|
| `TARGET_ADDR` | `localhost:8080` / `:8082` | Where to send packets |
| `NUM_WORKERS` | `5` | Parallel sender goroutines |
| `SEND_RATE` | `0` | Packets/sec.  `0` = burst (no rate limit) |
| `DURATION_SEC` | `5` | Duration in rate mode |
| `PKT_COUNT` | `20` | Packets to send in burst mode |
| `ASYNC_ACK` | `false` | `1` = fire-and-forget sends; ACK read in a goroutine.  Required for high-rate tests |

### stress_test.sh

| Variable | Default | Description |
|----------|---------|-------------|
| `STRESS_WORKERS` | `"1 2 4 8 16"` | Space-separated listener pool sizes to test |
| `PROBE_DURATION` | `5` | Seconds per measurement window |

---

## Metrics reference

### TCP listener (`job="tcp-listener"`, port 2112)

| Metric | Type | Description |
|--------|------|-------------|
| `tcp_connections_total` | counter | Connections accepted and fully handled |
| `tcp_connections_rejected_total` | counter | Connections turned away (`SERVER_BUSY`) |
| `tcp_connections_active` | gauge | Connections currently being processed |
| `tcp_process_goroutines` | gauge | Live goroutines (spikes under load in unbounded mode) |
| `tcp_process_rss_bytes` | gauge | Physical RAM used |
| `tcp_process_cpu_seconds_total` | counter | CPU time consumed |
| `tcp_process_bytes_sent_total` | counter | ACK bytes written to clients |
| `tcp_process_bytes_received_total` | counter | Payload bytes read from clients |
| `tcp_request_duration_ms_sum` | counter | Cumulative handler time (ms) |

### UDP listener (`job="udp-listener"`, port 2114)

Same shape, prefix `udp_`, with `udp_packets_*` instead of `tcp_connections_*`
and `udp_workers_active` instead of `tcp_connections_active`.

### Stress test (`job="node"`, scraped from textfile)

| Metric | Labels | Description |
|--------|--------|-------------|
| `stress_ceiling_pps` | `proto`, `workers` | Throughput ceiling discovered by stress_test.sh |
| `stress_speedup_factor` | `workers` | UDP ceiling / TCP ceiling |
| `stress_test_timestamp_seconds` | — | Unix timestamp of last run |

---

## Tuning tips

| Goal | Change |
|------|--------|
| Model a real handler (database call, etc.) | Set `HANDLER_DELAY_MS=50` in docker-compose |
| See goroutine explosion under TCP load | Keep `WORKER_POOL_SIZE=0` and watch the Goroutines panel |
| See TCP rejection under overload | Set `WORKER_POOL_SIZE=4 QUEUE_SIZE=8`, send at high rate |
| Raise UDP recv-loop ceiling | The only fix is multiple `ReadFromUDP` goroutines — the listener code currently has one |
| Faster stress test | Lower `PROBE_DURATION` (`--duration 3`) or test fewer worker counts |
