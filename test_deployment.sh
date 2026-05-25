#!/bin/bash
# Test Deployment Script
# Verifies that the Minimal Pro theme is working correctly

set -e

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== QUANTRA TERMINAL - DEPLOYMENT TEST ===${NC}"
echo ""

# Test 1: Check if servers are running
echo -e "${YELLOW}[1/5] Checking if servers are running...${NC}"
if ps aux | grep -E "(ws_server|auth_proxy)" | grep -v grep > /dev/null; then
    echo -e "${GREEN}✅ Servers are running${NC}"
else
    echo -e "${RED}❌ Servers are not running${NC}"
    echo "Run: ./start_local.sh"
    exit 1
fi

# Test 2: Check if ports are accessible
echo -e "${YELLOW}[2/5] Checking if ports are accessible...${NC}"
if curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/ | grep -q "302\|200"; then
    echo -e "${GREEN}✅ Auth proxy (8080) is accessible${NC}"
else
    echo -e "${RED}❌ Auth proxy (8080) is not accessible${NC}"
    exit 1
fi

if curl -s -o /dev/null -w "%{http_code}" http://localhost:8081/health | grep -q "200"; then
    echo -e "${GREEN}✅ WebSocket server (8081) is accessible${NC}"
else
    echo -e "${RED}❌ WebSocket server (8081) is not accessible${NC}"
    exit 1
fi

# Test 3: Check if CSS files are accessible
echo -e "${YELLOW}[3/5] Checking if CSS files are accessible...${NC}"
if curl -s http://localhost:8080/static/css/minimal-pro-theme.css | head -5 | grep -q "MINIMAL PRO"; then
    echo -e "${GREEN}✅ minimal-pro-theme.css is accessible${NC}"
else
    echo -e "${RED}❌ minimal-pro-theme.css is not accessible${NC}"
    exit 1
fi

if curl -s http://localhost:8080/static/css/components.css | head -5 | grep -q "COMPONENT"; then
    echo -e "${GREEN}✅ components.css is accessible${NC}"
else
    echo -e "${RED}❌ components.css is not accessible${NC}"
    exit 1
fi

# Test 4: Check if dashboard file exists and has been modified
echo -e "${YELLOW}[4/5] Checking if dashboard has been redesigned...${NC}"
if grep -q "minimal-pro-theme.css" dashboard_live.html; then
    echo -e "${GREEN}✅ Dashboard has Minimal Pro theme links${NC}"
else
    echo -e "${RED}❌ Dashboard does not have Minimal Pro theme links${NC}"
    exit 1
fi

if [ -f "dashboard_live.html.backup" ]; then
    echo -e "${GREEN}✅ Backup file exists${NC}"
else
    echo -e "${YELLOW}⚠️  No backup file found${NC}"
fi

# Test 5: Check file sizes
echo -e "${YELLOW}[5/5] Checking file sizes...${NC}"
DASHBOARD_SIZE=$(wc -c < dashboard_live.html)
THEME_SIZE=$(wc -c < static/css/minimal-pro-theme.css)
COMPONENTS_SIZE=$(wc -c < static/css/components.css)

echo -e "${GREEN}✅ dashboard_live.html: $(numfmt --to=iec-i --suffix=B $DASHBOARD_SIZE)${NC}"
echo -e "${GREEN}✅ minimal-pro-theme.css: $(numfmt --to=iec-i --suffix=B $THEME_SIZE)${NC}"
echo -e "${GREEN}✅ components.css: $(numfmt --to=iec-i --suffix=B $COMPONENTS_SIZE)${NC}"

echo ""
echo -e "${GREEN}=== ALL TESTS PASSED ===${NC}"
echo ""
echo -e "${YELLOW}Next Steps:${NC}"
echo "1. Open http://localhost:8080/ in your browser"
echo "2. Log in with your credentials"
echo "3. Test all features (filters, sorting, analysis panel, chat, etc.)"
echo "4. Check if the light theme looks good"
echo "5. Provide feedback!"
echo ""
echo -e "${GREEN}Dashboard is ready for testing! 🚀${NC}"
echo ""
