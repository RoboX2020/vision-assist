#!/bin/bash
# ============================================================
# Vision Assist — Stop Server
# ============================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$SCRIPT_DIR/server/server.pid"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

if [ ! -f "$PID_FILE" ]; then
    echo -e "${YELLOW}No server PID file found. Server may not be running.${NC}"
    # Try finding by process name
    PIDS=$(pgrep -f "python.*server.py" 2>/dev/null || true)
    if [ -n "$PIDS" ]; then
        echo -e "${YELLOW}Found server processes: $PIDS${NC}"
        echo -e "${YELLOW}Killing...${NC}"
        kill $PIDS 2>/dev/null || true
        echo -e "${GREEN}✅ Done${NC}"
    else
        echo -e "${GREEN}No server processes running.${NC}"
    fi
    exit 0
fi

PID=$(cat "$PID_FILE")

if kill -0 "$PID" 2>/dev/null; then
    echo -e "${YELLOW}Stopping Vision Assist server (PID: $PID)...${NC}"
    kill "$PID" 2>/dev/null
    sleep 2
    
    # Force kill if still alive
    if kill -0 "$PID" 2>/dev/null; then
        echo -e "${RED}Force killing...${NC}"
        kill -9 "$PID" 2>/dev/null || true
    fi
    
    rm -f "$PID_FILE"
    echo -e "${GREEN}✅ Server stopped.${NC}"
else
    echo -e "${GREEN}Server (PID: $PID) is not running.${NC}"
    rm -f "$PID_FILE"
fi
