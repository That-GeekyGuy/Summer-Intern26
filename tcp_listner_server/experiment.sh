#!/bin/bash
# experiment.sh
# runs TCP stress experiments by varying MODE, NUM_WORKERS, PKT_COUNT
# all config injected via ConfigMap — no code changes needed between rounds

# exit immediately if any command fails
# treat unset variables as errors
set -eu

# ─── output files ────────────────────────────────────────────────────────────
RESULTS="results.txt"
LISTENER_LOGS="listener-logs.txt"

# clear both files at the start of each run
> $RESULTS
> $LISTENER_LOGS

# write header row — printf for aligned columns
printf "%-12s | %-8s | %-8s | %-12s | %-10s | %s\n" \
  "MODE" "WORKERS" "PACKETS" "STATUS" "DURATION" "NOTES" >> $RESULTS
printf "%-12s | %-8s | %-8s | %-12s | %-10s | %s\n" \
  "────────────" "────────" "────────" "────────────" "──────────" "─────────────────" >> $RESULTS

# ─── setup ───────────────────────────────────────────────────────────────────
echo ""
echo "========================================"
echo " TCP STRESS EXPERIMENT"
echo "========================================"

# ensure configmap exists before anything else
# apply is idempotent — safe to run even if it already exists
echo "[SETUP] Applying configmap..."
kubectl apply -f configmap.yaml

# give the API server a moment to register the configmap
sleep 2

# ─── test rounds ─────────────────────────────────────────────────────────────
# format: "MODE NUM_WORKERS PKT_COUNT"
#
# persistent mode — one socket, many packets — finds per-socket limit
# per-conn mode   — one socket per packet   — finds concurrent socket limit
ROUNDS=(
  "persistent 1   100"
  "persistent 1   1000"
  "persistent 1   10000"
  "persistent 5   1000"
  "persistent 10  1000"
  "persistent 50  1000"
  "persistent 100 1000"
  "per-conn   5   50"
  "per-conn   10  50"
  "per-conn   50  50"
  "per-conn   100 50"
  "per-conn   200 50"
  "per-conn   500 50"
)

# track round number for readability
ROUND_NUM=0

for ROUND in "${ROUNDS[@]}"; do
  ROUND_NUM=$((ROUND_NUM + 1))

  # cut splits the string by space and extracts field N
  # -d' ' = delimiter is space
  # -f1   = first field, -f2 = second, -f3 = third
  # xargs trims whitespace from the extracted value
  MODE=$(echo    $ROUND | awk '{print $1}')
  WORKERS=$(echo $ROUND | awk '{print $2}')
  PACKETS=$(echo $ROUND | awk '{print $3}')

  echo ""
  echo "──────────────────────────────────────────"
  echo " Round $ROUND_NUM — mode=$MODE workers=$WORKERS packets=$PACKETS"
  echo "──────────────────────────────────────────"

  # patch the configmap with values for this round
  # --patch takes a JSON merge patch — only updates specified keys
  # \" escapes quotes inside the double-quoted string
  kubectl patch configmap sender-config \
    --patch "{\"data\":{\"NUM_WORKERS\":\"$WORKERS\",\"PKT_COUNT\":\"$PACKETS\",\"MODE\":\"$MODE\"}}"

  echo "[ROUND $ROUND_NUM] ConfigMap updated"

  # jobs are immutable in kubernetes — you cannot update a running job
  # must delete and recreate for each round
  # --ignore-not-found prevents error on first round when no job exists yet
  kubectl delete job tcp-sender --ignore-not-found
  echo "[ROUND $ROUND_NUM] Old job deleted"

  # wait for the job object to fully disappear from the API server
  # without this, the next apply might conflict with the terminating old job
  # || true means: don't exit the script if this command fails
  # (it fails when job doesn't exist, which is fine)
  kubectl wait --for=delete job/tcp-sender --timeout=30s 2>/dev/null || true
  echo "[ROUND $ROUND_NUM] Confirmed job gone"

  # record wall-clock start time in unix seconds
  START=$(date +%s)

  # create the new job — kubernetes pulls config from the configmap at pod start
  kubectl apply -f generator-job.yaml
  echo "[ROUND $ROUND_NUM] Job created — waiting for completion..."

  # wait for job to reach Complete condition
  # timeout=180s — if job takes longer than 3 minutes, give up
  # 2>/dev/null suppresses the "timed out" message from kubectl
  # || true prevents script exit on timeout
  kubectl wait --for=condition=complete job/tcp-sender --timeout=180s 2>/dev/null || true
  COMPLETE=$?
  # $? captures the exit code of the last command
  # 0 = condition met (complete), 1 = timed out

  # separately check if job reached Failed condition
  # short 5s timeout — if it already failed this returns instantly
  kubectl wait --for=condition=failed job/tcp-sender --timeout=5s 2>/dev/null || true
  FAIL=$?

  # record end time and calculate duration
  END=$(date +%s)
  DURATION=$((END - START))

  # determine status from exit codes
  if   [ $COMPLETE -eq 0 ]; then STATUS="SUCCESS"
  elif [ $FAIL -eq 0 ];     then STATUS="FAILED"
  else                           STATUS="TIMEOUT"
  fi

  # ── check for OOMKill ──────────────────────────────────────────────────────
  # jsonpath navigates the kubernetes JSON response
  # items[*]  = all pods matching the label selector
  # .status.containerStatuses[*].lastState.terminated.reason
  #           = why the last container run terminated
  # if listener was OOMKilled, that appears here
  OOM=$(kubectl get pods -l app=tcp-listener \
    -o jsonpath='{.items[*].status.containerStatuses[*].lastState.terminated.reason}' \
    2>/dev/null || echo "")

  NOTES=""
  if [[ "$OOM" == *"OOMKilled"* ]]; then
    NOTES="LISTENER_OOMKilled"
    STATUS="FAILED"
    echo "[ROUND $ROUND_NUM] WARNING: listener was OOMKilled"
  fi

  # ── capture sender logs ────────────────────────────────────────────────────
  # logs job/tcp-sender pulls logs from any pod owned by the job
  # --tail=100 limits to last 100 lines so we don't overflow the file
  SENDER_SUMMARY=$(kubectl logs job/tcp-sender --tail=100 2>/dev/null || echo "no logs available")

  # extract the TOTAL line from sender summary for the notes column
  TOTAL_LINE=$(echo "$SENDER_SUMMARY" | grep "TOTAL" || echo "")

  if [ -n "$TOTAL_LINE" ] && [ -z "$NOTES" ]; then
    # grep -oP extracts matching pattern
    # if grep not available on your system, use awk instead
    LOSS=$(echo "$TOTAL_LINE" | grep -oP 'loss_rate=\S+' || echo "")
    NOTES="$LOSS"
  fi

  # ── print result row ───────────────────────────────────────────────────────
  # tee -a writes to stdout AND appends to file simultaneously
  printf "%-12s | %-8s | %-8s | %-12s | %-10s | %s\n" \
    "$MODE" "$WORKERS" "$PACKETS" "$STATUS" "${DURATION}s" "$NOTES" | tee -a $RESULTS

  # ── save listener logs for this round ─────────────────────────────────────
  # {} groups multiple commands — all their output goes to the same redirect
  {
    echo "════════════════════════════════════════════════════════"
    echo " Round $ROUND_NUM | mode=$MODE workers=$WORKERS packets=$PACKETS | $STATUS"
    echo "════════════════════════════════════════════════════════"
    kubectl logs -l app=tcp-listener --tail=100 2>/dev/null || echo "no listener logs"
    echo ""
    echo "── sender summary ──────────────────────────────────────"
    echo "$SENDER_SUMMARY"
    echo ""
  } >> $LISTENER_LOGS
  # >> appends to file without overwriting previous rounds

  echo "[ROUND $ROUND_NUM] Done — status=$STATUS duration=${DURATION}s"

  # pause between rounds — lets TCP connections drain and ports release
  # without this, leftover connections from round N bleed into round N+1
  echo "[ROUND $ROUND_NUM] Cooling down 5s..."
  sleep 5

done

# ─── final report ─────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════"
echo " EXPERIMENT COMPLETE"
echo "════════════════════════════════════════"
echo ""
cat $RESULTS
echo ""
echo "Full listener logs → $LISTENER_LOGS"
echo "Results table      → $RESULTS"