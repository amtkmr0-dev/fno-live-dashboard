#!/bin/bash
# sync_to_gcp.sh - Sync local files to GCP production server
# Usage: ./sync_to_gcp.sh [--dry-run]

set -e

# Configuration
GCP_USER="amitkumar"
GCP_HOST="35.206.87.181"
GCP_PATH="~/deploy"
LOCAL_PATH="."

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== FNO Dashboard - Sync to GCP ===${NC}"
echo ""

# Check if dry-run mode
DRY_RUN=""
if [[ "$1" == "--dry-run" ]]; then
    DRY_RUN="--dry-run"
    echo -e "${YELLOW}Running in DRY-RUN mode (no files will be transferred)${NC}"
    echo ""
fi

# Files to sync
FILES=(
    # Auth & security layer
    "auth_proxy.py"
    "db.py"
    "security.py"

    # Market data & trading engine
    "ws_server.py"
    "upstox_ws_stream.py"
    "auto_paper_trader.py"
    "auto_trader.py"
    "data_recorder.py"
    "integration_handler.py"

    # OI thesis pipeline (NEW)
    "oi_thesis_tracker.py"
    "build_oi_thesis_report.py"
    "chart_features.py"
    "historical_data.py"
    "export_nifty_timeseries.py"
    "backup_timeseries_to_gcs.sh"

    # AI chat
    "chat_analysis.py"

    # Utilities
    "setup_auth.py"

    # Frontend pages
    "index.html"
    "dashboard_live.html"
    "login.html"
    "register.html"
    "profile.html"
    "paper_trades.html"
    "admin.html"
    "divergence.html"
    "rsi.html"
    "rsi-analysis.html"
    "sectors.html"
    "paper.html"
    "oi-thesis.html"          # NEW: OI thesis tracker page with export modal + vol surge filter
    "oi_thesis_cron.sh"       # NEW: daily cron script for verify→capture→report cycle
    "terms.html"
    "privacy.html"
    "disclaimer.html"
    "theme.css"
    "requirements.txt"
)

# Confirm before proceeding (skip in dry-run)
if [[ -z "$DRY_RUN" ]]; then
    echo -e "${YELLOW}This will sync ${#FILES[@]} files to ${GCP_USER}@${GCP_HOST}:${GCP_PATH}${NC}"
    echo ""
    read -p "Continue? (y/N): " -n 1 -r
    echo ""
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 1
    fi
    echo ""
fi

# Create backup on server (skip in dry-run)
if [[ -z "$DRY_RUN" ]]; then
    echo -e "${GREEN}Creating backup on server...${NC}"
    BACKUP_NAME="backup_$(date +%Y%m%d_%H%M%S).tar.gz"
    ssh ${GCP_USER}@${GCP_HOST} "cd ${GCP_PATH} && tar -czf ${BACKUP_NAME} *.py *.html *.css 2>/dev/null || true && echo 'Backup: ${BACKUP_NAME}'"
    echo -e "${GREEN}✓ Backup created: ${BACKUP_NAME}${NC}"
    echo ""
fi

# Sync files
echo -e "${GREEN}Syncing files...${NC}"
for file in "${FILES[@]}"; do
    if [[ -f "$file" ]]; then
        echo -n "  Uploading $file... "
        if [[ -n "$DRY_RUN" ]]; then
            echo -e "${YELLOW}[DRY-RUN]${NC}"
        else
            scp -q "$file" ${GCP_USER}@${GCP_HOST}:${GCP_PATH}/
            echo -e "${GREEN}✓${NC}"
        fi
    else
        echo -e "  ${YELLOW}⚠ Skipping $file (not found)${NC}"
    fi
done

# Sync static/ folder (CSS/JS assets needed by dashboard_live.html)
echo ""
echo -e "${GREEN}Syncing static/ folder...${NC}"
if [[ -d "static" ]]; then
    if [[ -n "$DRY_RUN" ]]; then
        echo -e "  ${YELLOW}[DRY-RUN] Would rsync static/ → ${GCP_USER}@${GCP_HOST}:${GCP_PATH}/static/${NC}"
    else
        rsync -az --delete static/ ${GCP_USER}@${GCP_HOST}:${GCP_PATH}/static/
        echo -e "  ${GREEN}✓ static/ synced${NC}"
    fi
else
    echo -e "  ${YELLOW}⚠ static/ folder not found, skipping${NC}"
fi

echo ""
echo -e "${GREEN}=== Sync Complete ===${NC}"
echo ""

if [[ -z "$DRY_RUN" ]]; then
    echo -e "${YELLOW}Next steps:${NC}"
    echo "  1. SSH to server: ssh ${GCP_USER}@${GCP_HOST}"
    echo "  2. Restart services:"
    echo "     sudo systemctl restart ws_server"
    echo "     sudo systemctl restart auth_proxy"
    echo "  3. Check logs:"
    echo "     tail -f ~/deploy/server.log"
    echo ""
    echo -e "${YELLOW}If deploying OI thesis pipeline for the first time:${NC}"
    echo "  4. Make cron script executable:"
    echo "     ssh ${GCP_USER}@${GCP_HOST} 'chmod +x ~/deploy/oi_thesis_cron.sh'"
    echo "  5. Add cron job (15:35 IST = 10:05 UTC, Mon-Fri):"
    echo "     ssh ${GCP_USER}@${GCP_HOST} 'crontab -l | grep -q oi_thesis_cron || (crontab -l 2>/dev/null; echo \"5 10 * * 1-5 cd ~/deploy && ./oi_thesis_cron.sh >> ~/deploy/oi_thesis.log 2>&1\") | crontab -'"
    echo ""
    echo -e "${YELLOW}Or run restart + status in one shot:${NC}"
    echo "  ssh ${GCP_USER}@${GCP_HOST} 'cd ~/deploy && sudo systemctl restart ws_server auth_proxy && sleep 2 && sudo systemctl status ws_server auth_proxy'"
else
    echo -e "${YELLOW}This was a dry-run. Run without --dry-run to actually sync files.${NC}"
fi

echo ""
