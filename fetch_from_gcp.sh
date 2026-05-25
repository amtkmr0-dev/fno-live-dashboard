#!/bin/bash
# fetch_from_gcp.sh - Fetch ws_server.py and config files from GCP server
# Usage: ./fetch_from_gcp.sh

set -e

# Configuration
GCP_USER="amitkumar"
GCP_HOST="34.132.142.58"
GCP_PATH="~/deploy"
LOCAL_PATH="."

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}=== Fetching files from GCP Server ===${NC}"
echo ""

# Files to fetch (that are missing locally)
FILES=(
    "ws_server.py"
    "config.env"
    "auth_config.json"
)

echo -e "${YELLOW}This will download the following files from production:${NC}"
for file in "${FILES[@]}"; do
    echo "  - $file"
done
echo ""

read -p "Continue? (y/N): " -n 1 -r
echo ""
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 1
fi

echo ""
echo -e "${GREEN}Downloading files...${NC}"

for file in "${FILES[@]}"; do
    echo -n "  Fetching $file... "
    if scp -q ${GCP_USER}@${GCP_HOST}:${GCP_PATH}/$file ${LOCAL_PATH}/ 2>/dev/null; then
        echo -e "${GREEN}✓${NC}"
    else
        echo -e "${YELLOW}⚠ Not found or permission denied${NC}"
    fi
done

echo ""
echo -e "${GREEN}=== Fetch Complete ===${NC}"
echo ""
echo -e "${YELLOW}⚠️  IMPORTANT:${NC}"
echo "  - config.env and auth_config.json contain sensitive data"
echo "  - These files are in .gitignore and should NEVER be committed"
echo "  - Use them for local development only"
echo ""
echo -e "${GREEN}Next steps:${NC}"
echo "  1. Review downloaded files"
echo "  2. Set up local virtual environment: python3 -m venv venv"
echo "  3. Install dependencies: pip install -r requirements.txt"
echo "  4. Run locally: python3 ws_server.py (terminal 1) && python3 auth_proxy.py (terminal 2)"
echo ""
