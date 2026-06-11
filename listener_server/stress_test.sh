#!/usr/bin/env bash
# stress_test.sh — discover TCP, UDP, and QUIC throughput ceilings
#
# Method: unlimited-rate burst generator (ASYNC_ACK=1 / SEND_RATE=∞) run in
# background for PROBE_DURATION seconds. Throughput is measured from the
# listener-side Prometheus metrics (packets/streams per second delivered),
# not from the generator. Generator worker count doubles until the listener-
# side rate plateaus (<10% improvement) — that plateau is the ceiling.
#
# Results written to:
#   results.txt                      — human-readable table (all 3 protocols)
#   stress-metrics/stress_test.prom  — Prometheus textfile scraped by Grafana
#
# Usage: ./stress_test.sh [options]
#   --workers  "1 2 4 8"   listener pool sizes to test (default: "1 2 4 8 16")
#   --duration N            probe window seconds (default: 5)
#   --proto    "tcp udp quic"  which protocols to run (default: all three)
#   --help
#
# Environment overrides: STRESS_WORKERS, PROBE_DURATION, STRESS_PROTOS
set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────
WORKERS_LIST="${STRESS_WORKERS:-1 2 4 8 16}"
PROBE_DURATION="${PROBE_DURATION:-5}"
STRESS_PROTOS="${STRESS_PROTOS:-tcp udp quic}"

# Ports — separate from docker-compose so both can run simultaneously.
TCP_LISTEN_ADDR=":8081"  ; TCP_METRICS_ADDR=":2113"
UDP_LISTEN_ADDR=":8083"  ; UDP_METRICS_ADDR=":2115"
QUIC_LISTEN_ADDR=":8085" ; QUIC_METRICS_ADDR=":2117"

BIN_DIR="/tmp"
TCP_LISTENER_BIN="$BIN_DIR/ls-tcp-listener"
TCP_GEN_BIN="$BIN_DIR/ls-tcp-gen"
UDP_LISTENER_BIN="$BIN_DIR/ls-udp-listener"
UDP_GEN_BIN="$BIN_DIR/ls-udp-gen"
QUIC_LISTENER_BIN="$BIN_DIR/ls-quic-listener"
QUIC_GEN_BIN="$BIN_DIR/ls-quic-gen"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESULTS_FILE="$SCRIPT_DIR/results.txt"
TEXTFILE_DIR="$SCRIPT_DIR/stress-metrics"
TEXTFILE="$TEXTFILE_DIR/stress_test.prom"

# ── Colour helpers ─────────────────────────────────────────────────────────────
RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[0;33m'
BLU='\033[0;34m'; MAG='\033[0;35m'; CYN='\033[0;36m'
BOLD='\033[1m'; RST='\033[0m'
say()  { printf "${BLU}[stress]${RST} %s\n" "$*"; }
ok()   { printf "${GRN}[ok]${RST}    %s\n" "$*"; }
warn() { printf "${YLW}[warn]${RST}  %s\n" "$*"; }
err()  { printf "${RED}[err]${RST}   %s\n" "$*" >&2; }

usage() {
  cat <<EOF
Usage: $0 [options]
  --workers  "1 2 4 8"    Listener pool sizes to test (default: "$WORKERS_LIST")
  --duration N             Probe window seconds       (default: $PROBE_DURATION)
  --proto   "tcp udp quic" Protocols to benchmark     (default: all three)
  --help
Environment: STRESS_WORKERS, PROBE_DURATION, STRESS_PROTOS
EOF
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --workers)  WORKERS_LIST="$2"; shift 2 ;;
    --duration) PROBE_DURATION="$2"; shift 2 ;;
    --proto)    STRESS_PROTOS="$2"; shift 2 ;;
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
  (cd "$SCRIPT_DIR/quic/listener" && go build -o "$QUIC_LISTENER_BIN" .)
  (cd "$SCRIPT_DIR/quic/generator" && go build -o "$QUIC_GEN_BIN" .)
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
    if [[ $attempts -ge 80 ]]; then
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

# Metric names per protocol
total_metric_for()  {
  case "$1" in
    tcp)  echo "tcp_connections_total" ;;
    udp)  echo "udp_packets_total" ;;
    quic) echo "quic_streams_total" ;;
  esac
}
reject_metric_for() {
  case "$1" in
    tcp)  echo "tcp_connections_rejected_total" ;;
    udp)  echo "udp_packets_rejected_total" ;;
    quic) echo "quic_streams_rejected_total" ;;
  esac
}

# ── Core probe ─────────────────────────────────────────────────────────────────
# probe <gen_bin> <proto> <target> <metrics_addr> <gen_workers>
# Sets globals PROBE_RATE (req/s from listener side) and PROBE_REJECTS (delta).
PROBE_RATE=0
PROBE_REJECTS=0

probe() {
  local gen_bin="$1" proto="$2" target="$3" metrics_addr="$4" gen_workers="$5"
  local total_metric; total_metric="$(total_metric_for  "$proto")"
  local reject_metric; reject_metric="$(reject_metric_for "$proto")"

  local port="${metrics_addr#:}"
  local before before_r after after_r
  before="$(fetch_metric "$port" "$total_metric")";   before="${before:-0}"
  before_r="$(fetch_metric "$port" "$reject_metric")"; before_r="${before_r:-0}"

  # Launch generator: unlimited rate, long enough it never exits on its own.
  if [[ "$proto" == "quic" ]]; then
    # QUIC uses connection pooling — a small fixed pool saturates the listener.
    # POOL_CONNS=4 gives 4 long-lived connections shared by gen_workers streams.
    local pool_conns=$(( gen_workers < 4 ? gen_workers : 4 ))
    NUM_WORKERS="$gen_workers" TARGET_ADDR="$target" \
      SEND_RATE="99999999" DURATION_SEC="999" POOL_CONNS="$pool_conns" \
      "$gen_bin" &>/dev/null &
  else
    # TCP and UDP: new connection/socket per request, async ACK
    NUM_WORKERS="$gen_workers" TARGET_ADDR="$target" \
      ASYNC_ACK="1" SEND_RATE="99999999" DURATION_SEC="999" \
      "$gen_bin" &>/dev/null &
  fi
  local gen_pid=$!

  sleep "$PROBE_DURATION"

  after="$(fetch_metric "$port" "$total_metric")";   after="${after:-$before}"
  after_r="$(fetch_metric "$port" "$reject_metric")"; after_r="${after_r:-$before_r}"

  kill "$gen_pid" 2>/dev/null; wait "$gen_pid" 2>/dev/null || true

  PROBE_RATE=$(( (after - before) / PROBE_DURATION ))
  PROBE_REJECTS=$(( after_r - before_r ))
}

# ── Ceiling discovery ──────────────────────────────────────────────────────────
# find_ceiling <gen_bin> <proto> <target> <metrics_addr> <start_gen_workers>
# Sets global CEILING to the throughput ceiling (req/s).
CEILING=0

proto_color() {
  case "$1" in tcp) echo "$BLU" ;; udp) echo "$MAG" ;; quic) echo "$GRN" ;; *) echo "$CYN" ;; esac
}

find_ceiling() {
  local gen_bin="$1" proto="$2" target="$3" metrics_addr="$4" start_workers="$5"
  local gen_workers="$start_workers"
  local prev_rate=0
  CEILING=0
  local COLOR; COLOR="$(proto_color "$proto")"

  printf "${COLOR}[%s]${RST}  Burst scan (gen workers: %d → ...):\n" "${proto^^}" "$gen_workers"

  while [[ $gen_workers -le 512 ]]; do
    printf "${COLOR}[%s]${RST}  → gen_workers=%-4d  " "${proto^^}" "$gen_workers"

    probe "$gen_bin" "$proto" "$target" "$metrics_addr" "$gen_workers"

    printf "listener: %7d req/s" "$PROBE_RATE"

    if [[ $PROBE_REJECTS -gt 0 ]]; then
      printf "  ${RED}(rejects=%d → saturated)${RST}\n" "$PROBE_REJECTS"
      CEILING=$(( prev_rate > 0 ? prev_rate : PROBE_RATE ))
      return
    fi
    printf "\n"

    if [[ $prev_rate -gt 0 ]]; then
      local threshold=$(( prev_rate + prev_rate / 10 ))
      if [[ $PROBE_RATE -le $threshold ]]; then
        printf "${COLOR}[%s]${RST}  Plateau at %d req/s (improvement < 10%%).\n" \
          "${proto^^}" "$PROBE_RATE"
        CEILING=$PROBE_RATE
        return
      fi
    fi

    prev_rate=$PROBE_RATE
    gen_workers=$(( gen_workers * 2 ))
  done

  CEILING=$prev_rate
  warn "${proto^^} never plateaued up to gen_workers=512. Ceiling ≥ $CEILING req/s."
}

# ── Result storage ─────────────────────────────────────────────────────────────
declare -A TCP_MAP
declare -A UDP_MAP
declare -A QUIC_MAP

# ── Helpers ───────────────────────────────────────────────────────────────────
run_proto() {
  local proto="$1" listener_bin="$2" gen_bin="$3"
  local listen_addr="$4" metrics_addr="$5" n="$6"
  local result_var="${proto^^}_MAP"

  say "Starting ${proto^^} listener (pool=$n)…"
  start_listener "$listener_bin" "$listen_addr" "$metrics_addr" "$n"

  # Initial gen_workers: start higher for UDP/QUIC (cheaper per-request)
  local start_workers
  case "$proto" in
    tcp)  start_workers=$(( n > 4  ? n : 4  )) ;;
    udp)  start_workers=$(( n > 8  ? n : 8  )) ;;
    quic) start_workers=$(( n > 4  ? n : 4  )) ;;
  esac

  find_ceiling "$gen_bin" "$proto" "127.0.0.1${listen_addr}" "$metrics_addr" "$start_workers"
  eval "${proto^^}_MAP[$n]=$CEILING"
  ok "${proto^^} ceiling (N=$n): ${BOLD}${CEILING} req/s${RST}"

  stop_listener; sleep 0.5
}

# ── Main ───────────────────────────────────────────────────────────────────────
main() {
  build_all
  mkdir -p "$TEXTFILE_DIR"

  local start_ts; start_ts="$(date '+%Y-%m-%d %H:%M:%S')"
  local run_tcp=false; local run_udp=false; local run_quic=false
  for p in $STRESS_PROTOS; do
    [[ "$p" == "tcp"  ]] && run_tcp=true
    [[ "$p" == "udp"  ]] && run_udp=true
    [[ "$p" == "quic" ]] && run_quic=true
  done

  printf "\n${BOLD}══════════════════════════════════════════════════════════════${RST}\n"
  printf "${BOLD}  Listener Server — TCP · UDP · QUIC Throughput Discovery${RST}\n"
  printf "${BOLD}  Started  : %s${RST}\n" "$start_ts"
  printf "${BOLD}  Workers  : %s  |  Probe window: %ds${RST}\n" "$WORKERS_LIST" "$PROBE_DURATION"
  printf "${BOLD}  Protocols: %s${RST}\n" "$STRESS_PROTOS"
  printf "${BOLD}  Note     : throughput measured from listener-side Prometheus metrics${RST}\n"
  printf "${BOLD}══════════════════════════════════════════════════════════════${RST}\n\n"

  for N in $WORKERS_LIST; do
    printf "${BOLD}── listener pool N_WORKERS=%-3d ──────────────────────────────${RST}\n" "$N"

    $run_tcp  && run_proto tcp  "$TCP_LISTENER_BIN"  "$TCP_GEN_BIN"  "$TCP_LISTEN_ADDR"  "$TCP_METRICS_ADDR"  "$N"
    $run_udp  && run_proto udp  "$UDP_LISTENER_BIN"  "$UDP_GEN_BIN"  "$UDP_LISTEN_ADDR"  "$UDP_METRICS_ADDR"  "$N"
    $run_quic && run_proto quic "$QUIC_LISTENER_BIN" "$QUIC_GEN_BIN" "$QUIC_LISTEN_ADDR" "$QUIC_METRICS_ADDR" "$N"

    printf "\n"
  done

  # ── Comparison table ────────────────────────────────────────────────────────
  print_table() {
    local sep; sep="$(printf '%.0s─' {1..90})"
    {
    printf "\n"
    printf "  TCP · UDP · QUIC Throughput Comparison — %s\n" "$(date '+%Y-%m-%d %H:%M:%S')"
    printf "  Probe duration: %ds per measurement window\n\n" "$PROBE_DURATION"
    printf "  %-10s  %-14s  %-14s  %-14s  %-10s  %-10s\n" \
      "N_WORKERS" "TCP req/s" "UDP req/s" "QUIC req/s" "UDP/TCP" "QUIC/TCP"
    printf "  %s\n" "$sep"

    for N in $WORKERS_LIST; do
      local tcp_c="${TCP_MAP[$N]:-0}"
      local udp_c="${UDP_MAP[$N]:-0}"
      local quic_c="${QUIC_MAP[$N]:-0}"
      local udp_speedup quic_speedup

      if [[ $tcp_c -gt 0 ]]; then
        udp_speedup="$(awk  "BEGIN{printf \"%.1fx\", $udp_c  / $tcp_c}")"
        quic_speedup="$(awk "BEGIN{printf \"%.1fx\", $quic_c / $tcp_c}")"
      else
        udp_speedup="∞"; quic_speedup="∞"
      fi

      printf "  %-10s  %-14s  %-14s  %-14s  %-10s  %-10s\n" \
        "$N" "$tcp_c" "$udp_c" "$quic_c" "$udp_speedup" "$quic_speedup"
    done

    printf "  %s\n\n" "$sep"
    printf "  TCP  : limited by kernel TCP handshake rate (~0.1ms/conn on localhost)\n"
    printf "  UDP  : limited by single ReadFromUDP goroutine (recv-loop bound)\n"
    printf "  QUIC : reuses connections; stream-open costs ~1µs vs TCP handshake ~100µs\n"
    printf "         TLS 1.3 is paid once per connection, not per stream\n\n"
    } | cat
  }

  print_table
  {
    printf "stress_test.sh results\n"
    printf "======================\n"
    print_table | sed 's/\x1B\[[0-9;]*m//g'
  } > "$RESULTS_FILE"
  ok "Written: $RESULTS_FILE"

  # ── Prometheus textfile ─────────────────────────────────────────────────────
  {
    printf "# HELP stress_ceiling_pps Listener throughput ceiling (req/s) from stress_test.sh\n"
    printf "# TYPE stress_ceiling_pps gauge\n"
    for N in $WORKERS_LIST; do
      $run_tcp  && printf "stress_ceiling_pps{proto=\"tcp\",workers=\"%s\"}  %s\n" "$N" "${TCP_MAP[$N]:-0}"
      $run_udp  && printf "stress_ceiling_pps{proto=\"udp\",workers=\"%s\"}  %s\n" "$N" "${UDP_MAP[$N]:-0}"
      $run_quic && printf "stress_ceiling_pps{proto=\"quic\",workers=\"%s\"} %s\n" "$N" "${QUIC_MAP[$N]:-0}"
    done
    printf "\n"

    printf "# HELP stress_speedup_factor Protocol ceiling / TCP ceiling from stress_test.sh\n"
    printf "# TYPE stress_speedup_factor gauge\n"
    for N in $WORKERS_LIST; do
      local tcp_c="${TCP_MAP[$N]:-0}"
      local udp_sp=0 quic_sp=0
      if [[ $tcp_c -gt 0 ]]; then
        $run_udp  && udp_sp="$(awk  "BEGIN{printf \"%.2f\", ${UDP_MAP[$N]:-0}  / $tcp_c}")"
        $run_quic && quic_sp="$(awk "BEGIN{printf \"%.2f\", ${QUIC_MAP[$N]:-0} / $tcp_c}")"
      fi
      $run_udp  && printf "stress_speedup_factor{proto=\"udp\",workers=\"%s\"}  %s\n"  "$N" "$udp_sp"
      $run_quic && printf "stress_speedup_factor{proto=\"quic\",workers=\"%s\"} %s\n" "$N" "$quic_sp"
    done
    printf "\n"

    printf "# HELP stress_test_timestamp_seconds Unix timestamp of last stress test run\n"
    printf "# TYPE stress_test_timestamp_seconds gauge\n"
    printf "stress_test_timestamp_seconds %s\n" "$(date +%s)"
  } > "$TEXTFILE"
  ok "Written: $TEXTFILE  (scraped by Grafana via node-exporter textfile collector)"

  local end_ts; end_ts="$(date '+%Y-%m-%d %H:%M:%S')"
  printf "${BOLD}══════════════════════════════════════════════════════════════${RST}\n"
  printf "${BOLD}  Finished: %s${RST}\n" "$end_ts"
  printf "${BOLD}══════════════════════════════════════════════════════════════${RST}\n\n"
}

trap 'stop_listener; exit 1' INT TERM
main "$@"
