#!/usr/bin/env bash
# oi_thesis_cron.sh — runs the daily verify→capture→fetch→report cycle.
# Intended to fire at ~15:35 IST on trading days (cron entry below).
#
# Steps in order:
#   1. verify yesterday's flags + capture today's flags (oi_thesis_tracker)
#   2. fetch & cache 90d daily OHLC for today's flagged stocks (Upstox API)
#   3. compute price-action features and write the markdown report
#
# Cron line (15:35 IST = 10:05 UTC):
#   5 10 * * 1-5  cd /Users/amitkumar/Desktop/FNO\ Dashboard/fno-live-dashboard \
#               && ./oi_thesis_cron.sh >> oi_thesis.log 2>&1

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

PYTHON="$REPO_DIR/venv/bin/python3"
[ -x "$PYTHON" ] || PYTHON="python3"

echo "----- $(date '+%Y-%m-%d %H:%M:%S %Z') -----"

# Step 1 — capture today's flags + verify yesterday's outcomes
echo "[1/3] OI thesis verify + capture..."
"$PYTHON" oi_thesis_tracker.py daily

# Step 2 — pull 90d daily candles for the flagged stocks (cached, idempotent).
# Failures here don't stop the report; the report falls back to "no chart data".
echo "[2/3] Fetching historical OHLC..."
if ! "$PYTHON" historical_data.py fetch_today_flags; then
    echo "  WARN: OHLC fetch failed (token expired? rate-limit?). Continuing with cached data."
fi

# Step 3 — build the merged report
echo "[3/3] Building report..."
"$PYTHON" build_oi_thesis_report.py

echo "Tracker stats:"
"$PYTHON" oi_thesis_tracker.py report 30
echo
