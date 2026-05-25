#!/bin/bash
# Start Local Development Environment
# Usage: ./start_local.sh

set -e

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== Quantra Terminal - Local Development ===${NC}"
echo ""

# Check if venv exists
if [ ! -d "venv" ]; then
    echo -e "${RED}❌ Virtual environment not found!${NC}"
    echo "Run: python3 -m venv venv && ./venv/bin/pip install -r requirements.txt"
    exit 1
fi

# Check if config files exist
if [ ! -f "config.env" ]; then
    echo -e "${RED}❌ config.env not found!${NC}"
    echo "Copy from config.env.example and add your Upstox token"
    exit 1
fi

if [ ! -f "auth_config.json" ]; then
    echo -e "${RED}❌ auth_config.json not found!${NC}"
    echo "Copy from auth_config.json.example"
    exit 1
fi

# Check if ports are available
if lsof -Pi :8080 -sTCP:LISTEN -t >/dev/null 2>&1 ; then
    echo -e "${YELLOW}⚠️  Port 8080 is already in use${NC}"
    echo "Kill process: lsof -ti:8080 | xargs kill -9"
    exit 1
fi

if lsof -Pi :8081 -sTCP:LISTEN -t >/dev/null 2>&1 ; then
    echo -e "${YELLOW}⚠️  Port 8081 is already in use${NC}"
    echo "Kill process: lsof -ti:8081 | xargs kill -9"
    exit 1
fi

echo -e "${GREEN}✅ All checks passed!${NC}"
echo ""
echo -e "${YELLOW}Starting servers...${NC}"
echo ""

# Start ws_server in background
echo -e "${GREEN}[1/2] Starting WebSocket Server (port 8081)...${NC}"
./venv/bin/python3 ws_server.py > ws_server_local.log 2>&1 &
WS_PID=$!
echo "      PID: $WS_PID"
sleep 2

# Check if ws_server started successfully
if ! ps -p $WS_PID > /dev/null; then
    echo -e "${RED}❌ WebSocket server failed to start!${NC}"
    echo "Check ws_server_local.log for errors"
    exit 1
fi

# Start auth_proxy in background
echo -e "${GREEN}[2/2] Starting Auth Proxy (port 8080)...${NC}"
./venv/bin/python3 auth_proxy.py > auth_proxy_local.log 2>&1 &
AUTH_PID=$!
echo "      PID: $AUTH_PID"
sleep 2

# Check if auth_proxy started successfully
if ! ps -p $AUTH_PID > /dev/null; then
    echo -e "${RED}❌ Auth proxy failed to start!${NC}"
    echo "Check auth_proxy_local.log for errors"
    kill $WS_PID 2>/dev/null
    exit 1
fi

echo ""
echo -e "${GREEN}✅ Both servers started successfully!${NC}"
echo ""
echo -e "${YELLOW}=== Server Information ===${NC}"
echo "  WebSocket Server: http://localhost:8081/"
echo "  Auth Proxy:       http://localhost:8080/"
echo "  Dashboard:        http://localhost:8080/"
echo ""
echo -e "${YELLOW}=== Process IDs ===${NC}"
echo "  ws_server.py:     $WS_PID"
echo "  auth_proxy.py:    $AUTH_PID"
echo ""
echo -e "${YELLOW}=== Logs ===${NC}"
echo "  WebSocket:        tail -f ws_server_local.log"
echo "  Auth Proxy:       tail -f auth_proxy_local.log"
echo ""
echo -e "${YELLOW}=== Stop Servers ===${NC}"
echo "  kill $WS_PID $AUTH_PID"
echo "  or run: ./stop_local.sh"
echo ""
echo -e "${GREEN}🚀 Ready! Open http://localhost:8080/ in your browser${NC}"
echo ""

# Save PIDs to file for stop script
echo "$WS_PID" > .ws_server.pid
echo "$AUTH_PID" > .auth_proxy.pid
