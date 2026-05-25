#!/bin/bash
# Stop Local Development Environment
# Usage: ./stop_local.sh

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}=== Stopping Quantra Terminal ===${NC}"
echo ""

# Read PIDs from files
if [ -f ".ws_server.pid" ]; then
    WS_PID=$(cat .ws_server.pid)
    if ps -p $WS_PID > /dev/null 2>&1; then
        echo "Stopping WebSocket Server (PID: $WS_PID)..."
        kill $WS_PID
        echo -e "${GREEN}✅ WebSocket Server stopped${NC}"
    else
        echo "WebSocket Server not running"
    fi
    rm .ws_server.pid
fi

if [ -f ".auth_proxy.pid" ]; then
    AUTH_PID=$(cat .auth_proxy.pid)
    if ps -p $AUTH_PID > /dev/null 2>&1; then
        echo "Stopping Auth Proxy (PID: $AUTH_PID)..."
        kill $AUTH_PID
        echo -e "${GREEN}✅ Auth Proxy stopped${NC}"
    else
        echo "Auth Proxy not running"
    fi
    rm .auth_proxy.pid
fi

# Fallback: kill by port
if lsof -Pi :8081 -sTCP:LISTEN -t >/dev/null 2>&1 ; then
    echo "Killing process on port 8081..."
    lsof -ti:8081 | xargs kill -9
fi

if lsof -Pi :8080 -sTCP:LISTEN -t >/dev/null 2>&1 ; then
    echo "Killing process on port 8080..."
    lsof -ti:8080 | xargs kill -9
fi

echo ""
echo -e "${GREEN}✅ All servers stopped${NC}"
