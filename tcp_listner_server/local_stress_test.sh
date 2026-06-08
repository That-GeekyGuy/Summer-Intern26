#!/usr/bin/env bash
# local_stress_test.sh
#
# SYMMETRIC RATE SATURATION EXPERIMENT
# ═════════════════════════════════════
# Both sender (generator) and receiver (listener) are bounded by N workers.
# N sweeps from 1 to MAX_WORKERS on both sides simultaneously.
#
# METHODOLOGY
# ───────────
# Listener delay = DELAY_MS per connection  →  max throughput = 1000/DELAY_MS per worker.
# With DELAY_MS=10ms: 1 worker handles 100 pkts/sec, N workers handle N×100 pkts/sec.
#
# Generator sends at a controlled rate (SEND_RATE pkts/sec) for PROBE_DURATION seconds.
# Workers are async so the ticker drives the send rate, not ACK round-trips.
#
# Phase 1 — Exponential rate scan: 10 → 20 → 40 → 80 … pkts/sec until first drop.
# Phase 2 — Binary search in rate space: narrows the exact saturation point.
#
# Expected result: saturation at exactly N × 100 pkts/sec (linear scaling).

set -euo pipefail

# ─── tuning knobs ─────────────────────────────────────────────────────────────
LISTENER_PORT=8081
METRICS_PORT=2113
DELAY_MS=10        # ms per connection handler → capacity = 1000/DELAY_MS = 100 pkts/sec/worker
QUEUE_SIZE=5       # buffer absorbs ticker jitter at exactly capacity; see listener note
# time.Sleep(10ms) on Linux overshoots by ~0.5ms (1ms timer resolution), so
# true capacity ≈ 94/s per worker, not 100/s. QUEUE_SIZE=5 absorbs jitter AT
# capacity; a 1-pkt/s excess fills the queue in 5s. PROBE_DURATION=8s gives
# 3s of observable drops after the queue fills → reliable detection of +1 excess.
PROBE_DURATION=8   # seconds per rate probe — must be > QUEUE_SIZE / min_excess
MAX_RATE=2048      # safety ceiling on exponential rate scan
MAX_WORKERS=5      # sweep N = 1 → MAX_WORKERS on both sides

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESULTS="$BASE/local_results.txt"
LISTENER_LOG="$BASE/local_listener.log"
> "$LISTENER_LOG"

RATE_PER_WORKER=$(( 1000 / DELAY_MS ))   # = 100

# ─── banner ───────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║   TCP RATE SATURATION — Symmetric Bottleneck Discovery       ║"
echo "║                                                              ║"
printf "║   Sender = Listener = N workers  (N sweeps 1 → %d)         ║\n" "$MAX_WORKERS"
printf "║   Delay  = %dms  →  capacity = N × %d pkts/sec             ║\n" \
    "$DELAY_MS" "$RATE_PER_WORKER"
printf "║   Queue  = %d (fixed)  |  Probe = %ds per rate              ║\n" \
    "$QUEUE_SIZE" "$PROBE_DURATION"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "[BUILD] Compiling listener and generator..."
(cd "$BASE/Listener"  && go build -o /tmp/tcp-stress-listener  .)
(cd "$BASE/Generator" && go build -o /tmp/tcp-stress-generator .)
echo "[BUILD] Done."
echo ""

LISTENER_PID=""
cleanup() {
    [ -n "$LISTENER_PID" ] && kill "$LISTENER_PID" 2>/dev/null || true
    wait "$LISTENER_PID" 2>/dev/null || true
}
trap cleanup EXIT

# ─── probe_rate ───────────────────────────────────────────────────────────────
# Send at RATE pkts/sec using WORKERS async generator workers for PROBE_DURATION s.
# Prints three tokens: success  busy  loss_rate
probe_rate() {
    local rate=$1 workers=$2 port=$3
    local out
    out=$(TARGET_ADDR="localhost:$port" \
          NUM_WORKERS="$workers" \
          SEND_RATE="$rate" \
          DURATION_SEC="$PROBE_DURATION" \
          /tmp/tcp-stress-generator 2>&1) || true

    local line
    line=$(echo "$out" | grep "TOTAL:" | tail -1 || echo "")
    [ -z "$line" ] && { echo "0 0 100.00%"; return; }

    local s b l
    s=$(echo "$line" | grep -oP 'success=\K\d+'        || echo "0")
    b=$(echo "$line" | grep -oP 'busy=\K\d+'           || echo "0")
    l=$(echo "$line" | grep -oP 'loss_rate=\K[0-9.]+%' || echo "?")
    echo "$s $b $l"
}

has_drops() { [ "$1" -gt 0 ]; }

# ─── results header ───────────────────────────────────────────────────────────
> "$RESULTS"
{
printf "# Rate saturation experiment — %s\n" "$(date)"
printf "# Delay=%dms  →  capacity = N × %d pkts/sec\n" "$DELAY_MS" "$RATE_PER_WORKER"
printf "# Queue=%d (fixed)  Probe=%ds  Both sides = N workers\n" "$QUEUE_SIZE" "$PROBE_DURATION"
printf "#\n"
printf "%-9s | %-13s | %-13s | %-13s | %s\n" \
    "N_WORKERS" "THEORY" "LAST_CLEAN" "FIRST_DROP" "THEORY CHECK"
printf "%-9s | %-13s | %-13s | %-13s | %s\n" \
    "─────────" "─────────────" "─────────────" "─────────────" "────────────────────────"
} | tee -a "$RESULTS"

# ─── main sweep: N = 1 → MAX_WORKERS ─────────────────────────────────────────
for N in $(seq 1 "$MAX_WORKERS"); do

    THEORY_RATE=$(( N * RATE_PER_WORKER ))  # N × 100

    [ -n "$LISTENER_PID" ] && {
        kill "$LISTENER_PID" 2>/dev/null || true
        wait "$LISTENER_PID" 2>/dev/null || true
    }
    sleep 1

    WORKER_POOL_SIZE=$N \
    QUEUE_SIZE=$QUEUE_SIZE \
    HANDLER_DELAY_MS=$DELAY_MS \
    LISTEN_ADDR=":$LISTENER_PORT" \
    METRICS_ADDR=":$METRICS_PORT" \
        /tmp/tcp-stress-listener 2>>"$LISTENER_LOG" &
    LISTENER_PID=$!
    sleep 1

    if ! kill -0 "$LISTENER_PID" 2>/dev/null; then
        echo "ERROR: listener failed to start for N=$N. Check $LISTENER_LOG"; exit 1
    fi

    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    printf "║  N = %-2s workers on each side                               ║\n" "$N"
    printf "║  Listener : pool=%-2s  queue=%d  delay=%dms                   ║\n" \
        "$N" "$QUEUE_SIZE" "$DELAY_MS"
    printf "║  Generator: %d async workers  |  probe = %ds per rate        ║\n" \
        "$N" "$PROBE_DURATION"
    printf "║  Theory   : capacity = %d × %d = %d pkts/sec                ║\n" \
        "$N" "$RATE_PER_WORKER" "$THEORY_RATE"
    echo "╚══════════════════════════════════════════════════════════════╝"
    echo ""
    echo "  Phase 1 — Exponential rate scan  (10, 20, 40 … pkts/sec)"
    echo "  ──────────────────────────────────────────────────────────"

    # ── Phase 1: exponential rate scan ───────────────────────────────────────
    rate=10
    last_ok_rate=0
    first_fail_rate=0
    scan_done=0

    while [ "$rate" -le "$MAX_RATE" ]; do
        read -r succ busy loss <<< "$(probe_rate "$rate" "$N" "$LISTENER_PORT")"
        ts=$(date +%H:%M:%S)
        if has_drops "$busy"; then
            printf "  [%s]  rate=%-6s/s  ✗ DROPS   success=%-5s  busy=%-5s  loss=%s\n" \
                "$ts" "$rate" "$succ" "$busy" "$loss"
            first_fail_rate=$rate
            scan_done=1
            break
        else
            printf "  [%s]  rate=%-6s/s  ✓ clean   success=%-5s  loss=%s\n" \
                "$ts" "$rate" "$succ" "$loss"
            last_ok_rate=$rate
        fi
        rate=$((rate * 2))
    done

    if [ "$scan_done" -eq 0 ]; then
        echo ""
        echo "  WARNING: no saturation below MAX_RATE=$MAX_RATE/s at N=$N"
        printf "%-9s | %-13s | %-13s | %-13s | %s\n" \
            "$N" "${THEORY_RATE}/s" "${last_ok_rate}/s" ">$MAX_RATE/s" \
            "STABLE — no drop found below $MAX_RATE/s" \
            | tee -a "$RESULTS"
        continue
    fi

    echo ""
    printf "  → Bracketed: last_clean=%s/s   first_drop=%s/s\n" \
        "$last_ok_rate" "$first_fail_rate"
    echo ""
    echo "  Phase 2 — Binary search  [$last_ok_rate/s, $first_fail_rate/s]"
    echo "  ──────────────────────────────────────────────────────────"

    # ── Phase 2: binary search in rate space ─────────────────────────────────
    lo=$last_ok_rate
    hi=$first_fail_rate

    while [ $((hi - lo)) -gt 1 ]; do
        mid=$(( (lo + hi) / 2 ))
        read -r succ busy loss <<< "$(probe_rate "$mid" "$N" "$LISTENER_PORT")"
        ts=$(date +%H:%M:%S)
        if has_drops "$busy"; then
            printf "  [%s]  rate=%-6s/s  ✗ DROPS   success=%-5s  busy=%-5s  loss=%s\n" \
                "$ts" "$mid" "$succ" "$busy" "$loss"
            hi=$mid
        else
            printf "  [%s]  rate=%-6s/s  ✓ clean   success=%-5s  loss=%s\n" \
                "$ts" "$mid" "$succ" "$loss"
            lo=$mid
        fi
    done

    # ── theory check (10% tolerance for OS timer jitter) ─────────────────────
    # time.Sleep overshoots by ~0.5ms on Linux → empirical ≈ 94/s vs theory 100/s
    DELTA=$((lo - THEORY_RATE))
    ABS_DELTA=${DELTA#-}
    TOLERANCE=$(( THEORY_RATE / 10 ))  # 10%
    if [ "$ABS_DELTA" -le "$TOLERANCE" ]; then
        THEORY_CHECK="✓ ~${lo}/s  (theory=${THEORY_RATE}/s, delta=$DELTA)"
    else
        THEORY_CHECK="⚠ theory=${THEORY_RATE}/s  actual=${lo}/s  (delta=$DELTA)"
    fi

    echo ""
    echo "  ┌──────────────────────────────────────────────────────────────┐"
    printf "  │  N=%-2s  Theory = %d × %d = %d pkts/sec                   │\n" \
        "$N" "$N" "$RATE_PER_WORKER" "$THEORY_RATE"
    printf "  │       Last clean rate : %d pkts/sec                        │\n" "$lo"
    printf "  │       First drop rate : %d pkts/sec                        │\n" "$hi"
    printf "  │       %-53s│\n" "$THEORY_CHECK"
    echo "  └──────────────────────────────────────────────────────────────┘"
    echo ""

    printf "%-9s | %-13s | %-13s | %-13s | %s\n" \
        "$N" "${THEORY_RATE}/s" "${lo}/s" "${hi}/s" "$THEORY_CHECK" \
        | tee -a "$RESULTS"

    sleep 1
done

# ─── final summary ────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║   RATE SATURATION EXPERIMENT — COMPLETE                      ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
cat "$RESULTS"
echo ""
echo "Listener log  → $LISTENER_LOG"
echo "Results table → $RESULTS"
