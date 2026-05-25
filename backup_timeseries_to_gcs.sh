#!/usr/bin/env bash
# backup_timeseries_to_gcs.sh — nightly: extract Nifty OI timeseries to CSV.gz and upload to GCS.
#
# Usage:
#   ./backup_timeseries_to_gcs.sh
#
# Cron suggestion (15:45 IST = 10:15 UTC, after market close):
#   45 10 * * 1-5  cd /path/to/fno-live-dashboard && ./backup_timeseries_to_gcs.sh >> timeseries_backup.log 2>&1

set -euo pipefail

# --- Config ---
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUCKET="gs://quantra-history-backups/timeseries"
PYTHON="$REPO_DIR/venv/bin/python3"

# Fall back to system python3 if venv missing
if [ ! -x "$PYTHON" ]; then
    PYTHON="python3"
fi

echo "[ts-backup] Starting Nifty Timeseries Extraction... $(date)"

# --- Step 1: Run Python Extractor ---
"$PYTHON" "$REPO_DIR/export_nifty_timeseries.py"

# --- Step 2: Upload to GCP ---
DATESTAMP=$(date +%Y-%m-%d)
TARGET_FILE="$REPO_DIR/data/exports/nifty_timeseries_${DATESTAMP}.csv.gz"

if [ ! -f "$TARGET_FILE" ]; then
    echo "[ts-backup] WARNING: Target file $TARGET_FILE not found. Perhaps no data today?"
    exit 0
fi

if command -v gcloud >/dev/null 2>&1; then
    GCLOUD="gcloud storage cp"
elif command -v gsutil >/dev/null 2>&1; then
    GCLOUD="gsutil cp"
else
    echo "[ts-backup] ERROR: neither gcloud nor gsutil found on PATH"
    exit 1
fi

DEST_OBJ="$BUCKET/YYYY/MM/DD/nifty_timeseries_${DATESTAMP}.csv.gz"
# Replace YYYY/MM/DD with actual
YEAR=$(date +%Y)
MONTH=$(date +%m)
DAY=$(date +%d)
DEST_OBJ="$BUCKET/$YEAR/$MONTH/$DAY/nifty_timeseries_${DATESTAMP}.csv.gz"

echo "[ts-backup] Uploading CSV to $DEST_OBJ"
$GCLOUD "$TARGET_FILE" "$DEST_OBJ"

echo "[ts-backup] Done. $(date)"
