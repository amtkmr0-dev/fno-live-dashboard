#!/usr/bin/env bash
# backup_to_gcs.sh — nightly: roll up EOD, gzip the SQLite DB, upload to GCS.
#
# Usage:
#   ./backup_to_gcs.sh
#
# Cron suggestion (16:00 IST = 10:30 UTC, after market close + chain settle):
#   30 10 * * 1-5  cd /path/to/fno-live-dashboard && ./backup_to_gcs.sh >> backup.log 2>&1
#
# Requires: gcloud (or gsutil) on PATH and authenticated for the target bucket.

set -euo pipefail

# --- Config ---
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB_PATH="$REPO_DIR/data/quantra_history.db"
BUCKET="gs://quantra-history-backups"
PYTHON="$REPO_DIR/venv/bin/python3"

# Fall back to system python3 if venv missing
if [ ! -x "$PYTHON" ]; then
    PYTHON="python3"
fi

# --- Sanity ---
if [ ! -f "$DB_PATH" ]; then
    echo "[backup] No DB at $DB_PATH yet — nothing to back up. Skipping."
    exit 0
fi

# --- Step 1: EOD rollup so today's data is in stock_daily ---
echo "[backup] Running EOD rollup..."
"$PYTHON" "$REPO_DIR/data_recorder.py" rollup || {
    echo "[backup] WARN: rollup failed, continuing with backup anyway"
}

# --- Step 2: Cleanup snapshots older than 30 days (long history kept in stock_daily) ---
echo "[backup] Pruning old snapshots..."
"$PYTHON" "$REPO_DIR/data_recorder.py" cleanup || {
    echo "[backup] WARN: cleanup failed, continuing"
}

# --- Step 3: gzip the DB to a temp file ---
TS=$(date -u +"%Y%m%dT%H%M%SZ")
DATESTAMP=$(date +%Y-%m-%d)
TMP="$(mktemp -t quantra_history_XXXX).db.gz"

echo "[backup] Compressing $DB_PATH..."
gzip -c "$DB_PATH" > "$TMP"
SIZE=$(du -h "$TMP" | awk '{print $1}')
echo "[backup] Compressed size: $SIZE"

# --- Step 4: upload — keep both a 'latest' pointer and a dated copy ---
DATED_OBJ="$BUCKET/daily/$DATESTAMP/quantra_history_$TS.db.gz"
LATEST_OBJ="$BUCKET/latest/quantra_history.db.gz"

if command -v gcloud >/dev/null 2>&1; then
    GCLOUD="gcloud storage cp"
elif command -v gsutil >/dev/null 2>&1; then
    GCLOUD="gsutil cp"
else
    echo "[backup] ERROR: neither gcloud nor gsutil found on PATH"
    rm -f "$TMP"
    exit 1
fi

echo "[backup] Uploading dated copy → $DATED_OBJ"
$GCLOUD "$TMP" "$DATED_OBJ"

echo "[backup] Updating latest pointer → $LATEST_OBJ"
$GCLOUD "$TMP" "$LATEST_OBJ"

rm -f "$TMP"

echo "[backup] Done. $(date)"
