#!/usr/bin/env bash
# stress_test.sh — discover TCP and UDP throughput ceilings
#
# Method: unlimited-rate burst generator (ASYNC_ACK=1) run in background
# for PROBE_DURATION seconds. Throughput is measured from listener-side
# Prometheus metrics (packets-per-second delivered to the listener), not
# from the generator side. Generator-worker count doubles until the listener-
# side rate plateaus (<10% improvement), at which point the plateau is the
# throughput ceiling.
#
# Results are written to:
#   results.txt                            — human-readable table
#   stress-metrics/stress_test.prom        — Prometheus textfile for Grafana
#
# Usage: ./stress_test.sh [options]
#   --workers "1 2 4 8"   listener pool sizes to test (default: "1 2 4 8 16")
#   --duration N           seconds each measurement window lasts (default: 5)
#   --help
#
# Environment overrides: STRESS_WORKERS, PROBE_DURATION
set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────
WORKERS_LIST="${STRESS_WORKERS:-1 2 3 4 5}"
PROBE_DURATION="${PROBE_DURATION:-5}"

# Ports — intentionally different from docker-compose (8080/2112, 8082/2114)
# so stress test and Docker stack can run simultaneously.
TCP_LISTEN_ADDR=":8081"
TCP_METRICS_ADDR=":2113"
UDP_LISTEN_ADDR=":8083"
UDP_METRICS_ADDR=":2115"

BIN_DIR="/tmp"
TCP_LISTENER_BIN="$BIN_DIR/ls-tcp-listener"
TCP_GEN_BIN="$BIN_DIR/ls-tcp-gen"
UDP_LISTENER_BIN="$BIN_DIR/ls-udp-listener"
UDP_GEN_BIN="$BIN_DIR/ls-udp-gen"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESULTS_FILE="$SCRIPT_DIR/results.txt"
TEXTFILE_DIR="$SCRIPT_DIR/stress-metrics"
TEXTFILE="$TEXTFILE_DIR/stress_test.prom"

# ── Colour helpers ─────────────────────────────────────────────────────────────
RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[0;33m'; BLU='\033[0;34m'
MAG='\033[0;35m'; BOLD='\033[1m'; RST='\033[0m'
say()  { printf "${BLU}[stress]${RST} %s\n" "$*"; }
ok()   { printf "${GRN}[ok]${RST}    %s\n" "$*"; }
warn() { printf "${YLW}[warn]${RST}  %s\n" "$*"; }
err()  { printf "${RED}[err]${RST}   %s\n" "$*" >&2; }

usage() {
  cat <<EOF
Usage: $0 [options]
  --workers "1 2 4 8"   Listener pool sizes to test (default: "$WORKERS_LIST")
  --duration N           Probe window seconds (default: $PROBE_DURATION)
  --help
Environment: STRESS_WORKERS, PROBE_DURATION
EOF
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --workers)  WORKERS_LIST="$2"; shift 2 ;;
    --duration) PROBE_DURATION="$2"; shift 2 ;;
    --help)     usage ;;
    *) err "Unknown argument: $1"; usage ;;
  esac
done

# ── Build ──────────────────────────────────────────────────────────────────────
build_all() {
  say "Building binaries…"
  (cd "$SCRIPT_DIR/tcp/listener"  && go build -o "$TCP_LISTENER_BIN" .)
  (cd "$SCRIPT_DIR/tcp/generator" && go build -o "$TCP_GEN_BIN" .)
  (cd "$SCRIPT_DIR/udp/listener"  && go build -o "$UDP_LISTENER_BIN" .)
  (cd "$SCRIPT_DIR/udp/generator" && go build -o "$UDP_GEN_BIN" .)
  ok "Binaries ready."
}

# ── Process management ─────────────────────────────────────────────────────────
LISTENER_PID=""

start_listener() {
  local bin="$1" listen_addr="$2" metrics_addr="$3" workers="$4"
  LISTEN_ADDR="$listen_addr" METRICS_ADDR="$metrics_addr" \
    WORKER_POOL_SIZE="$workers" QUEUE_SIZE="512" HANDLER_DELAY_MS="0" \
    "$bin" &
  LISTENER_PID=$!
  local port="${metrics_addr#:}"
  local attempts=0
  while ! curl -sf "http://127.0.0.1:$port/metrics" >/dev/null 2>&1; do
    sleep 0.1; (( attempts++ ))
    if [[ $attempts -ge 50 ]]; then
      err "Listener didn't start (metrics :$port)"; kill "$LISTENER_PID" 2>/dev/null; return 1
    fi
  done
}

stop_listener() {
  if [[ -n "$LISTENER_PID" ]]; then
    kill "$LISTENER_PID" 2>/dev/null || true
    wait "$LISTENER_PID" 2>/dev/null || true
    LISTENER_PID=""
  fi
}

# ── Metric helpers ─────────────────────────────────────────────────────────────
fetch_metric() {
  local port="${1#:}"
  curl -sf "http://127.0.0.1:$port/metrics" 2>/dev/null \
    | grep -E "^${2}[{ ]" | awk '{print $NF}' | head -1
}

# ── Core probe ─────────────────────────────────────────────────────────────────
# probe <gen_bin> <proto> <target> <metrics_addr> <gen_workers>
# Sets globals PROBE_RATE (pkt/s from listener side) and PROBE_REJECTS (delta).
PROBE_RATE=0
PROBE_REJECTS=0

probe() {
  local gen_bin="$1" proto="$2" target="$3" metrics_addr="$4" gen_workers="$5"
  local total_metric reject_metric
  if [[ "$proto" == "tcp" ]]; then
    total_metric="tcp_connections_total"
    reject_metric="tcp_connections_rejected_total"
  else
    total_metric="udp_packets_total"
    reject_metric="udp_packets_rejected_total"
  fi

  local port="${metrics_addr#:}"
  local before after before_r after_r
  before="$(fetch_metric "$port" "$total_metric")";  before="${before:-0}"
  before_r="$(fetch_metric "$port" "$reject_metric")"; before_r="${before_r:-0}"

  # Run generator: unlimited rate (SEND_RATE=99999999 ≈ no throttle),
  # async ACK, long enough DURATION_SEC that it never exits on its own.
  # We kill it after PROBE_DURATION to get a clean measurement window.
  NUM_WORKERS="$gen_workers" TARGET_ADDR="$target" \
    ASYNC_ACK="1" SEND_RATE="99999999" DURATION_SEC="999" \
    "$gen_bin" &>/dev/null &
  local gen_pid=$!

  sleep "$PROBE_DURATION"

  # Sample while generator is still running — clean window of exactly PROBE_DURATION s
  after="$(fetch_metric "$port" "$total_metric")";  after="${after:-$before}"
  after_r="$(fetch_metric "$port" "$reject_metric")"; after_r="${after_r:-$before_r}"

  kill "$gen_pid" 2>/dev/null; wait "$gen_pid" 2>/dev/null || true

  PROBE_RATE=$(( (after - before) / PROBE_DURATION ))
  PROBE_REJECTS=$(( after_r - before_r ))
}

# ── Ceiling discovery ──────────────────────────────────────────────────────────
# find_ceiling <gen_bin> <proto> <target> <metrics_addr> <start_gen_workers>
# Sets global CEILING to the throughput ceiling (pkt/s).
CEILING=0

find_ceiling() {
  local gen_bin="$1" proto="$2" target="$3" metrics_addr="$4" start_workers="$5"
  local gen_workers="$start_workers"
  local prev_rate=0
  CEILING=0
  local COLOR="$BLU"; [[ "$proto" == "udp" ]] && COLOR="$MAG"

  printf "${COLOR}[%s]${RST}  Burst scan (gen workers: %d → ...):\n" "${proto^^}" "$gen_workers"

  while [[ $gen_workers -le 512 ]]; do
    printf "${COLOR}[%s]${RST}  → gen_workers=%-4d  " "${proto^^}" "$gen_workers"

    probe "$gen_bin" "$proto" "$target" "$metrics_addr" "$gen_workers"

    printf "listener: %7d pkt/s" "$PROBE_RATE"

    if [[ $PROBE_REJECTS -gt 0 ]]; then
      printf "  ${RED}(rejects=%d → saturated)${RST}\n" "$PROBE_REJECTS"
      # Use last clean rate as ceiling; if first probe already had rejects, use this rate.
      CEILING=$(( prev_rate > 0 ? prev_rate : PROBE_RATE ))
      return
    fi
    printf "\n"

    if [[ $prev_rate -gt 0 ]]; then
      # Check for plateau: less than 10% improvement when doubling workers.
      local threshold=$(( prev_rate + prev_rate / 10 ))
      if [[ $PROBE_RATE -le $threshold ]]; then
        printf "${COLOR}[%s]${RST}  Plateau at %d pkt/s (improvement < 10%%).\n" \
          "${proto^^}" "$PROBE_RATE"
        CEILING=$PROBE_RATE
        return
      fi
    fi

    prev_rate=$PROBE_RATE
    gen_workers=$(( gen_workers * 2 ))
  done

  # Never plateaued — report highest measured rate.
  CEILING=$prev_rate
  warn "${proto^^} never plateaued up to gen_workers=512. Ceiling ≥ $CEILING pkt/s."
}

# ── Result storage ─────────────────────────────────────────────────────────────
declare -A TCP_MAP   # TCP_MAP[$n] = ceiling pkt/s
declare -A UDP_MAP

# ── Main ───────────────────────────────────────────────────────────────────────
main() {
  build_all
  mkdir -p "$TEXTFILE_DIR"

  local start_ts
  start_ts="$(date '+%Y-%m-%d %H:%M:%S')"

  printf "\n${BOLD}════════════════════════════════════════════════════════════${RST}\n"
  printf "${BOLD}  Listener Server — TCP vs UDP Throughput Discovery${RST}\n"
  printf "${BOLD}  Started : %s${RST}\n" "$start_ts"
  printf "${BOLD}  Workers : %s  |  Probe window: %ds${RST}\n" "$WORKERS_LIST" "$PROBE_DURATION"
  printf "${BOLD}  Note    : throughput measured from listener-side Prometheus metrics${RST}\n"
  printf "${BOLD}════════════════════════════════════════════════════════════${RST}\n\n"

  for N in $WORKERS_LIST; do
    printf "${BOLD}── listener pool N_WORKERS=%-3d ─────────────────────────────${RST}\n" "$N"

    # ── TCP ───────────────────────────────────────────────────────────────────
    say "Starting TCP listener (pool=$N)…"
    start_listener "$TCP_LISTENER_BIN" "$TCP_LISTEN_ADDR" "$TCP_METRICS_ADDR" "$N"

    find_ceiling "$TCP_GEN_BIN" "tcp" "127.0.0.1${TCP_LISTEN_ADDR}" \
                 "$TCP_METRICS_ADDR" "$(( N > 4 ? N : 4 ))"
    TCP_MAP[$N]=$CEILING
    ok "TCP ceiling: ${BOLD}${CEILING} pkt/s${RST}"

    stop_listener; sleep 0.5

    # ── UDP ───────────────────────────────────────────────────────────────────
    say "Starting UDP listener (pool=$N)…"
    start_listener "$UDP_LISTENER_BIN" "$UDP_LISTEN_ADDR" "$UDP_METRICS_ADDR" "$N"

    # UDP starts probing with more gen_workers because each socket costs ~5µs
    # vs TCP's ~0.5ms handshake — we need more parallelism to load the listener.
    find_ceiling "$UDP_GEN_BIN" "udp" "127.0.0.1${UDP_LISTEN_ADDR}" \
                 "$UDP_METRICS_ADDR" "$(( N > 8 ? N : 8 ))"
    UDP_MAP[$N]=$CEILING
    ok "UDP ceiling: ${BOLD}${CEILING} pkt/s${RST}"

    stop_listener; sleep 0.5
    printf "\n"
  done

  # ── Print comparison table ─────────────────────────────────────────────────
  print_table() {
    local header
    header="$(printf "%-12s  %-16s  %-16s  %-10s  %s" \
      "N_WORKERS" "TCP ceiling/s" "UDP ceiling/s" "Speedup" "Note")"

    local sep; sep="$(printf '%.0s─' {1..72})"

    {
    printf "\n"
    printf "  TCP vs UDP Throughput Comparison — %s\n" "$(date '+%Y-%m-%d %H:%M:%S')"
    printf "  Probe duration: %ds per measurement window\n\n" "$PROBE_DURATION"
    printf "  %s\n" "$header"
    printf "  %s\n" "$sep"

    for N in $WORKERS_LIST; do
      local tcp_c="${TCP_MAP[$N]:-0}"
      local udp_c="${UDP_MAP[$N]:-0}"
      local speedup note

      if [[ $tcp_c -gt 0 ]]; then
        speedup="$(awk "BEGIN{printf \"%.1fx\", $udp_c / $tcp_c}")"
      else
        speedup="∞"
      fi

      if [[ $udp_c -ge $tcp_c && $N -gt 1 ]]; then
        local prev=$(( N / 2 ))
        local prev_udp="${UDP_MAP[$prev]:-0}"
        if [[ $udp_c -le $(( prev_udp + prev_udp / 10 )) && $prev_udp -gt 0 ]]; then
          note="UDP recv-loop bound (single goroutine)"
        else
          note="—"
        fi
      else
        note="—"
      fi

      printf "  %-12s  %-16s  %-16s  %-10s  %s\n" \
        "$N" "$tcp_c" "$udp_c" "$speedup" "$note"
    done

    printf "  %s\n" "$sep"
    printf "\n"
    printf "  TCP ceiling: handshake cost (~0.1ms/conn on localhost) limits goroutine-per-conn throughput\n"
    printf "  UDP ceiling: single ReadFromUDP goroutine in listener is the recv-loop bottleneck\n"
    printf "  Both generators use new-socket-per-packet; UDP Dial() is a local syscall (~1-5us)\n\n"
    } | cat
  }

  # Print to terminal with colours, write stripped copy to results.txt
  print_table
  {
    printf "stress_test.sh results\n"
    printf "======================\n"
    print_table | sed 's/\x1B\[[0-9;]*m//g'
  } > "$RESULTS_FILE"
  ok "Written: $RESULTS_FILE"

  # ── Write Prometheus textfile for Grafana ──────────────────────────────────
  {
    printf "# HELP stress_ceiling_pps Listener throughput ceiling (pkt/s) from stress_test.sh\n"
    printf "# TYPE stress_ceiling_pps gauge\n"
    for N in $WORKERS_LIST; do
      printf "stress_ceiling_pps{proto=\"tcp\",workers=\"%s\"} %s\n" "$N" "${TCP_MAP[$N]:-0}"
      printf "stress_ceiling_pps{proto=\"udp\",workers=\"%s\"} %s\n" "$N" "${UDP_MAP[$N]:-0}"
    done
    printf "\n"
    printf "# HELP stress_speedup_factor UDP/TCP throughput ratio from stress_test.sh\n"
    printf "# TYPE stress_speedup_factor gauge\n"
    for N in $WORKERS_LIST; do
      local tcp_c="${TCP_MAP[$N]:-0}"
      local udp_c="${UDP_MAP[$N]:-0}"
      local speedup=0
      [[ $tcp_c -gt 0 ]] && speedup="$(awk "BEGIN{printf \"%.2f\", $udp_c / $tcp_c}")"
      printf "stress_speedup_factor{workers=\"%s\"} %s\n" "$N" "$speedup"
    done
    printf "\n"
    printf "# HELP stress_test_timestamp_seconds Unix timestamp of last stress test run\n"
    printf "# TYPE stress_test_timestamp_seconds gauge\n"
    printf "stress_test_timestamp_seconds %s\n" "$(date +%s)"
  } > "$TEXTFILE"
  ok "Written: $TEXTFILE  (auto-scraped by Grafana via node-exporter textfile collector)"

  local end_ts; end_ts="$(date '+%Y-%m-%d %H:%M:%S')"
  printf "${BOLD}════════════════════════════════════════════════════════════${RST}\n"
  printf "${BOLD}  Finished: %s${RST}\n" "$end_ts"
  printf "${BOLD}════════════════════════════════════════════════════════════${RST}\n\n"
}

trap 'stop_listener; exit 1' INT TERM
main "$@"
