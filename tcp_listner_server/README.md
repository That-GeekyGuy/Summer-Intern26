# TCP listener server

A Go TCP server and traffic generator with a rate-saturation stress test and a Prometheus + Grafana monitoring stack. The project measures **sustained connection throughput** (complete TCP handshakes per second) and confirms that a bounded worker pool scales linearly with worker count.

All commands below assume your shell is in `tcp_listner_server/`.

---

## Overview

The core question this project answers:

> Given a TCP server where each connection handler takes a fixed amount of time, how many complete TCP connections per second can N workers sustain — and does that number scale linearly?

Each "packet" in the test is one **complete TCP connection lifecycle**: 3-way handshake → message write → ACK read → connection close. This is not a single TCP segment — it is the full round-trip cost of opening and closing a connection.

Key findings from the stress test (10 ms handler delay):

| N workers | Theoretical max | Empirical max | Per-worker rate |
|-----------|----------------|---------------|-----------------|
| 1 | 100/s | **94/s** | 94/s |
| 2 | 200/s | **186/s** | 93/s |
| 3 | 300/s | **288/s** | 96/s |
| 4 | 400/s | **377/s** | 94/s |
| 5 | 500/s | **465/s** | 93/s |

Throughput is 6–7% below theory because `time.Sleep(10ms)` on Linux overshoots by ~0.5ms per call (1 ms kernel timer resolution). Scaling is linear to within measurement noise.

---

## How a connection works

Each packet sent by the generator is one complete TCP session:

```
Generator                          Listener
─────────                          ────────
net.DialTimeout() ── SYN ────────→
                  ←─ SYN-ACK ─────
                  ── ACK ─────────→  (3-way handshake complete)

conn.Write("Hello...\n") ─────────→ bufio.ReadString('\n')
                                     time.Sleep(DELAY_MS)
                  ←─ "ACK N: ..." ── conn.Write(ack)
                                     conn.Close()  ← listener initiates
                  ←─ FIN ───────────
                  ── FIN-ACK ───────→
```

The listener always initiates the close. This frees the worker slot immediately after the ACK write, rather than waiting for the client's FIN — which was the bottleneck in an earlier design that used `bufio.Scanner`.

---

## Architecture

Two independent Go modules, linked by a `go.work` workspace:

```
Generator (N async workers)          Listener (N pool workers)
───────────────────────────          ──────────────────────────
time.Ticker at SEND_RATE/s           net.Accept loop
  │                                    │
  ▼                                    ├─ queue <- conn       (OK: queued)
jobsCh <- pkgid                        └─ queue full?
  │                                        conn.Write("SERVER_BUSY\n")
  ▼                                        conn.Close()       (rejected)
worker.dial(TARGET_ADDR)
worker.write(message)              pool worker:
readAck() goroutine ──────────→      handleConn(conn, DELAY_MS)
  reads "ACK N" or "SERVER_BUSY"       ReadString('\n')
                                        Sleep(DELAY_MS)
                                        Write("ACK N: ...")
                                        Close()
```

**Symmetric bottleneck**: both sides run with the same `N` workers. The listener's throughput ceiling is `N × (1000/DELAY_MS)` pkts/sec. The generator's ticker fires independently of ACK round-trips, so it can exceed that ceiling and force observable drops.

---

## Project layout

```
tcp_listner_server/
├── Listener/                    # TCP server (Go module)
│   ├── main.go
│   ├── Dockerfile
│   └── go.mod
├── Generator/                   # Traffic generator (Go module)
│   ├── main.go
│   ├── Dockerfile
│   └── go.mod
├── k8s/                         # Kubernetes manifests
│   ├── configmap.yaml
│   ├── generator-job.yaml
│   ├── listener-deployment.yaml
│   ├── listener-service.yaml
│   ├── prometheus.yaml
│   ├── node-exporter.yaml
│   └── grafana.yaml
├── monitoring/
│   ├── prometheus/prometheus.yml
│   └── grafana/
│       ├── provisioning/
│       │   ├── dashboards/dashboards.yml
│       │   └── datasources/prometheus.yml
│       └── dashboards/tcp-listener.json
├── docker-compose.yaml          # Listener + Prometheus + Grafana + node-exporter
├── local_stress_test.sh         # Rate saturation experiment (no Docker/K8s needed)
├── experiment.sh                # Multi-round Kubernetes experiment runner
├── go.work                      # Go workspace linking both modules
└── .gitignore
```

---

## Prerequisites

| Tool | Purpose |
|------|---------|
| Go 1.21+ | Build and run locally |
| Docker + Docker Compose | Containerised monitoring stack |
| minikube + kubectl | Kubernetes option |

---

## Configuration

### Listener (`Listener/main.go`)

| Variable | Default | Description |
|----------|---------|-------------|
| `LISTEN_ADDR` | `:8080` | TCP listen address |
| `METRICS_ADDR` | `:2112` | Prometheus `/metrics` HTTP address |
| `WORKER_POOL_SIZE` | `0` | Max concurrent handlers. `0` = one goroutine per connection (unbounded). |
| `QUEUE_SIZE` | `POOL × 2` | Pending-connection buffer before `SERVER_BUSY`. Only active when `WORKER_POOL_SIZE > 0`. |
| `HANDLER_DELAY_MS` | `0` | Artificial per-connection delay in ms. Sets the throughput ceiling: `1000 / DELAY_MS` pkts/sec/worker. |

### Generator (`Generator/main.go`)

| Variable | Default | Description |
|----------|---------|-------------|
| `TARGET_ADDR` | `localhost:8080` | Listener address |
| `NUM_WORKERS` | `5` | Sender goroutines |
| `SEND_RATE` | `0` | Pkts/sec to send. `0` = burst mode. Any positive value activates rate mode. |
| `DURATION_SEC` | `5` | Duration of rate-mode probe in seconds |
| `PKT_COUNT` | `20` | Total packets to send (burst mode only) |
| `ASYNC_ACK` | `false` | Fire-and-forget in burst mode. In rate mode this is always `true`. |

---

## Option A — run locally

**Terminal 1 — start the listener**
```bash
cd Listener && go run .
```
```
TCP listener on :8080 | pool=0 queue=0 delay=0ms
Metrics endpoint: http://0.0.0.0:2112/metrics
```

**Terminal 2 — send traffic**
```bash
TARGET_ADDR=localhost:8080 NUM_WORKERS=3 PKT_COUNT=20 go run ./Generator/
```
```
start burst mode: target=localhost:8080 workers=3 packets=20 asyncACK=false
[worker 0] response: ACK 1: Got your message
...
TOTAL: sent=20 success=20 busy=0 timeout=0 error=0 loss_rate=0.00%
```

**Check raw metrics**
```bash
curl localhost:2112/metrics
```

---

## Option B — Docker Compose (with monitoring)

```bash
docker compose up -d
```

| URL | Credentials |
|-----|-------------|
| http://localhost:3000 | admin / admin (Grafana) |
| http://localhost:9090 | — (Prometheus) |
| http://localhost:2112/metrics | — (raw metrics) |

The Grafana dashboard auto-provisions — no import needed.

Send traffic:
```bash
TARGET_ADDR=localhost:8080 NUM_WORKERS=5 PKT_COUNT=100 go run ./Generator/
```

Stop:
```bash
docker compose down
```

---

## Option C — Kubernetes (minikube)

**Start minikube and build the image inside it**
```bash
minikube start
eval $(minikube docker-env)
docker build -t xtremevoid/tcp-listener:v3 ./Listener/
eval $(minikube docker-env --unset)
```

**Deploy listener and monitoring**
```bash
kubectl apply -f k8s/listener-deployment.yaml
kubectl apply -f k8s/listener-service.yaml
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/node-exporter.yaml
kubectl apply -f k8s/prometheus.yaml
kubectl apply -f k8s/grafana.yaml
kubectl get pods -w
```

**Open dashboards**
```bash
minikube service grafana --url      # http://<ip>:30300  (admin/admin)
minikube service prometheus --url   # http://<ip>:30909
```

**Run the generator job**
```bash
kubectl apply -f k8s/generator-job.yaml
kubectl logs -f job/tcp-sender
```

**Tear down**
```bash
kubectl delete -f k8s/
```

---

## Stress test

### Design: symmetric bottleneck

The stress test uses a **rate-based symmetric bottleneck**:

- Both the generator and the listener are configured with exactly `N` workers
- `N` is swept from 1 to 5 on both sides simultaneously
- The listener uses `HANDLER_DELAY_MS=10`, giving a theoretical ceiling of `N × 100` pkts/sec
- The generator sends at a controlled rate via `time.Ticker`, independent of ACK round-trips

This is fundamentally different from a burst test. A burst test fires a fixed number of packets and measures how many fit in the pool + queue simultaneously — that measures queue depth, not throughput. The rate test measures **sustained connections per second**, which is the operationally relevant number.

### Why async ACK matters

In synchronous mode each generator worker waits for an ACK before sending the next packet. The round-trip time naturally rate-limits the sender (one packet per RTT per worker), so it can never exceed the server's throughput ceiling — you never see drops regardless of the packet count.

Rate mode fixes this: the `time.Ticker` fires at the target rate, workers use `asyncACK=true` to read responses in background goroutines, and the ticker is the sole pacemaker. This decouples send rate from round-trip latency and lets the generator exceed the listener's capacity at will.

### Methodology

**Phase 1 — Exponential scan**: probe rates 10, 20, 40, 80, 160… pkts/sec until the first drop (`busy > 0`) is observed. This brackets the failure range.

**Phase 2 — Binary search**: probe the midpoint of `[last_clean, first_drop]`, halving the range each step until the two bounds are 1 pkts/sec apart. This pinpoints the exact saturation rate.

Each probe runs for `PROBE_DURATION=8` seconds. A `QUEUE_SIZE=5` absorbs brief jitter; at 1 pkt/s above capacity the queue fills in 5 seconds, leaving 3 seconds of observable drops to confirm the detection.

### Running the stress test

```bash
./local_stress_test.sh
```

The script compiles both binaries to `/tmp/`, starts a fresh listener per `N`, and prints the full scan. No Docker or Kubernetes required.

To watch live metrics while the test runs:

```bash
# Terminal 1
docker compose up -d

# Terminal 2
./local_stress_test.sh
```

Open http://localhost:3000 — rejected/s spikes the moment the send rate exceeds capacity.

### Sample output (N=2)

```
╔══════════════════════════════════════════════════════════════╗
║  N = 2  workers on each side                                 ║
║  Listener : pool=2  queue=5  delay=10ms                      ║
║  Generator: 2 async workers  |  probe = 8s per rate          ║
║  Theory   : capacity = 2 × 100 = 200 pkts/sec               ║
╚══════════════════════════════════════════════════════════════╝

  Phase 1 — Exponential rate scan  (10, 20, 40 … pkts/sec)
  ──────────────────────────────────────────────────────────
  [15:06:39]  rate=10    /s  ✓ clean   success=80    loss=0.00%
  [15:06:48]  rate=20    /s  ✓ clean   success=160   loss=0.00%
  [15:06:57]  rate=40    /s  ✓ clean   success=320   loss=0.00%
  [15:07:06]  rate=80    /s  ✓ clean   success=640   loss=0.00%
  [15:07:15]  rate=160   /s  ✓ clean   success=1280  loss=0.00%
  [15:07:24]  rate=320   /s  ✗ DROPS   success=1487  busy=25   loss=1.65%

  → Bracketed: last_clean=160/s   first_drop=320/s

  Phase 2 — Binary search  [160/s, 320/s]
  ──────────────────────────────────────────────────────────
  [15:07:33]  rate=240   /s  ✗ DROPS   success=1874  busy=46   loss=2.40%
  [15:07:42]  rate=200   /s  ✗ DROPS   success=1594  busy=5    loss=0.31%
  [15:07:51]  rate=180   /s  ✓ clean   success=1440  loss=0.00%
  [15:08:00]  rate=190   /s  ✗ DROPS   success=1519  busy=1    loss=0.07%
  [15:08:09]  rate=185   /s  ✓ clean   success=1480  loss=0.00%
  [15:08:18]  rate=187   /s  ✗ DROPS   success=1495  busy=1    loss=0.07%
  [15:08:27]  rate=186   /s  ✓ clean   success=1488  loss=0.00%

  ┌──────────────────────────────────────────────────────────────┐
  │  N=2   Theory = 2 × 100 = 200 pkts/sec                       │
  │        Last clean rate : 186 pkts/sec                        │
  │        First drop rate : 187 pkts/sec                        │
  │        ✓ ~186/s  (theory=200/s, delta=-14)                   │
  └──────────────────────────────────────────────────────────────┘
```

### Results summary

Empirical results (`DELAY_MS=10`, `QUEUE_SIZE=5`):

| N workers | Theory | Last clean | First drop | Check |
|-----------|--------|------------|------------|-------|
| 1 | 100/s | **94/s** | 95/s | ✓ within 10% |
| 2 | 200/s | **186/s** | 187/s | ✓ within 10% |
| 3 | 300/s | **288/s** | 289/s | ✓ within 10% |
| 4 | 400/s | **377/s** | 378/s | ✓ within 10% |
| 5 | 500/s | **465/s** | 466/s | ✓ within 10% |

Per-worker throughput: 93–96 pkts/sec consistently. The 6% gap below theory comes from `time.Sleep(10ms)` overshooting by ~0.5ms on Linux (1 ms kernel timer resolution), making the effective handler time ~10.5ms instead of 10ms.

### Theory check tolerance

The test applies a 10% tolerance (not 5%) when comparing empirical results to theory. The rationale:

- `time.Sleep` overshoots by ~0.5ms → effective rate is ~94/s vs 100/s theory → 6% gap
- At `N=5` the accumulated jitter is `5 × 0.5ms = 2.5ms` per concurrent slot
- 10% tolerance cleanly covers all `N` without false failures

---

## Monitoring

### Prometheus metrics (`localhost:2112/metrics`)

The listener exposes a custom text-format `/metrics` endpoint with no external Prometheus client library.

| Metric | Type | Description |
|--------|------|-------------|
| `tcp_connections_total` | counter | Connections accepted and handled |
| `tcp_connections_rejected_total` | counter | Connections rejected with `SERVER_BUSY` |
| `tcp_connections_active` | gauge | Connections currently being handled |
| `tcp_request_duration_ms_sum` | counter | Total handler time across all connections (ms) |
| `tcp_process_cpu_seconds_total` | counter | CPU time used by this process (from `/proc/self/stat`) |
| `tcp_process_rss_bytes` | gauge | Physical RAM used by this process (from `/proc/self/status`) |
| `tcp_process_heap_alloc_bytes` | gauge | Go heap bytes currently allocated |
| `tcp_process_goroutines` | gauge | Live goroutine count |
| `tcp_process_uptime_seconds` | gauge | Seconds since process start |
| `tcp_process_bytes_sent_total` | counter | Total bytes written to clients |
| `tcp_process_bytes_received_total` | counter | Total bytes read from clients |

### Grafana dashboard

The dashboard auto-loads when Docker Compose starts. It has 12 panels in three rows.

**Row 1 — stat tiles**

| Panel | Description |
|-------|-------------|
| CPU usage | Current CPU % used by this process |
| RAM usage | Current physical RAM (RSS) |
| Goroutines | Live goroutine count |
| Active connections | Connections being handled right now |
| Total connections | Running total of accepted connections |
| Rejected connections | Running total of `SERVER_BUSY` rejections |

**Row 2 — process resource graphs**

| Panel | Description |
|-------|-------------|
| CPU usage over time | Per-process CPU % over time |
| RAM usage over time | RSS + Go heap alloc over time |
| Network bytes/s | Bytes sent and received per second |

**Row 3 — connection graphs**

| Panel | Description |
|-------|-------------|
| Goroutines over time | Goroutine count over time |
| Active connections over time | Active connection count over time |
| Connections/s vs rejections/s | Throughput vs rejection rate side-by-side |

**What to watch during the stress test:**
- `Rejected/s` spikes when the send rate first exceeds the worker capacity
- `Active connections` saturates at exactly `WORKER_POOL_SIZE` and stays flat
- `Goroutines` stays low because the pool is bounded — no goroutine explosion under load

---

## Quick reference

| Task | Command |
|------|---------|
| Start listener | `cd Listener && go run .` |
| Send a burst | `TARGET_ADDR=localhost:8080 PKT_COUNT=50 go run ./Generator/` |
| Send at a fixed rate | `TARGET_ADDR=localhost:8080 SEND_RATE=80 DURATION_SEC=10 go run ./Generator/` |
| Run stress test | `./local_stress_test.sh` |
| Start monitoring stack | `docker compose up -d` |
| Open Grafana | http://localhost:3000 (admin / admin) |
| Open Prometheus | http://localhost:9090 |
| Check raw metrics | `curl localhost:2112/metrics` |
| Stop Docker stack | `docker compose down` |
| Run K8s experiment | `./experiment.sh` |
| Deploy to K8s | `kubectl apply -f k8s/` |
| Tear down K8s | `kubectl delete -f k8s/` |
