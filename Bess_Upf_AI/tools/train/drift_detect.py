#!/usr/bin/env python3
"""
Drift detection script.

Reads the anomaly_feedback table from the audit database to determine the
operator-reported false positive rate. If the false positive rate exceeds
a defined threshold over a recent time window, it triggers a retrain.
"""

import argparse
import logging
import sqlite3
import sys
import subprocess
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

DRIFT_THRESHOLD = 0.15  # 15% false positive rate triggers retrain
MIN_FEEDBACK_EVENTS = 5 # Need at least 5 feedback events to evaluate drift

def evaluate_drift(db_path: str, hours: int = 24) -> bool:
    """Evaluate drift based on recent feedback."""
    cutoff_ts = int((datetime.now() - timedelta(hours=hours)).timestamp())
    
    try:
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        cur.execute(
            "SELECT is_true_positive, COUNT(*) FROM anomaly_feedback WHERE created_at >= ? GROUP BY is_true_positive",
            (cutoff_ts,)
        )
        rows = cur.fetchall()
        con.close()
    except Exception as e:
        log.error("Failed to read feedback from %s: %s", db_path, e)
        return False

    true_positives = 0
    false_positives = 0
    for is_tp, count in rows:
        if is_tp == 1:
            true_positives += count
        else:
            false_positives += count

    total_feedback = true_positives + false_positives
    if total_feedback < MIN_FEEDBACK_EVENTS:
        log.info("Not enough feedback events in the last %d hours to evaluate drift (found %d, need %d).", hours, total_feedback, MIN_FEEDBACK_EVENTS)
        return False

    fp_rate = false_positives / total_feedback
    log.info("Feedback summary (last %d hours): %d TP, %d FP. False Positive Rate: %.2f%%", hours, true_positives, false_positives, fp_rate * 100)

    if fp_rate >= DRIFT_THRESHOLD:
        log.warning("⚠ DRIFT DETECTED! False positive rate %.2f%% exceeds threshold %.2f%%.", fp_rate * 100, DRIFT_THRESHOLD * 100)
        return True
    
    log.info("False positive rate is within acceptable limits.")
    return False

def trigger_retrain():
    """Trigger the retraining pipeline."""
    log.info("Triggering prepare_dataset.py...")
    subprocess.run([sys.executable, "train/prepare_dataset.py"], check=True)
    
    log.info("Triggering train.py...")
    subprocess.run([sys.executable, "train/train.py"], check=True)
    
    log.info("Retraining pipeline completed successfully.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", required=True, help="Path to the audit SQLite database containing anomaly_feedback")
    parser.add_argument("--hours", type=int, default=24, help="Time window in hours to evaluate feedback")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate drift without triggering retrain")
    args = parser.parse_args()

    drift_detected = evaluate_drift(args.db_path, args.hours)
    
    if drift_detected and not args.dry_run:
        trigger_retrain()
