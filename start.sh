#!/bin/bash
# ============================================================
# Vision Assist — Auto-Launcher Script
# ============================================================
# This script:
# 1. Detects your Mac's IP on the network (phone hotspot)
# 2. Updates ESP32 config.h with the correct IP
# 3. Starts the server in the background
# 4. Prints the URLs to connect from your phone
#
# Usage: chmod +x start.sh && ./start.sh
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVER_DIR="$SCRIPT_DIR/server"
FIRMWARE_DIR="$SCRIPT_DIR/esp32_firmware"
CONFIG_FILE="$FIRMWARE_DIR/config.h"
LOG_FILE="$SERVER_DIR/server.log"
PID_FILE="$SERVER_DIR/server.pid"
VENV_DIR="$SERVER_DIR/venv"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
PURPLE='\033[0;35m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color
BOLD='\033[1m'

echo ""
echo -e "${PURPLE}${BOLD}╔══════════════════════════════════════════╗${NC}"
echo -e "${PURPLE}${BOLD}║     🔮 Vision Assist — Auto-Launcher     ║${NC}"
echo -e "${PURPLE}${BOLD}╚══════════════════════════════════════════╝${NC}"
echo ""

# ---- Step 0: Kill any existing server ----
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE" 2>/dev/null)
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        echo -e "${YELLOW}⚠️  Stopping existing server (PID: $OLD_PID)${NC}"
        kill "$OLD_PID" 2>/dev/null || true
        sleep 1
    fi
    rm -f "$PID_FILE"
fi

# ---- Step 1: Detect IP Address ----
echo -e "${CYAN}📡 Detecting network IP address...${NC}"

# Try multiple methods to find the IP
LOCAL_IP=""

# Method 1: Route-based detection (most reliable)
if [ -z "$LOCAL_IP" ]; then
    LOCAL_IP=$(python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(('8.8.8.8', 80))
    print(s.getsockname()[0])
except:
    pass
finally:
    s.close()
" 2>/dev/null)
fi

# Method 2: ifconfig on macOS
if [ -z "$LOCAL_IP" ]; then
    LOCAL_IP=$(ifconfig | grep 'inet ' | grep -v '127.0.0.1' | head -1 | awk '{print $2}')
fi

# Method 3: Fallback
if [ -z "$LOCAL_IP" ]; then
    LOCAL_IP="127.0.0.1"
    echo -e "${RED}⚠️  Could not detect IP. Using localhost.${NC}"
    echo -e "${YELLOW}   Make sure your Mac is connected to your phone's hotspot.${NC}"
fi

echo -e "${GREEN}✅ Server IP: ${BOLD}$LOCAL_IP${NC}"
echo ""

# ---- Step 2: Update ESP32 config.h ----
if [ -f "$CONFIG_FILE" ]; then
    echo -e "${CYAN}📝 Updating ESP32 config.h with IP: $LOCAL_IP${NC}"
    # Use sed to update SERVER_HOST
    if [[ "$(uname)" == "Darwin" ]]; then
        sed -i '' "s/#define SERVER_HOST.*/#define SERVER_HOST \"$LOCAL_IP\" \/\/ Automatically updated/" "$CONFIG_FILE"
    else
        sed -i "s/#define SERVER_HOST.*/#define SERVER_HOST \"$LOCAL_IP\" \/\/ Automatically updated/" "$CONFIG_FILE"
    fi
    echo -e "${GREEN}✅ config.h updated${NC}"
else
    echo -e "${YELLOW}⚠️  config.h not found, skipping ESP32 update${NC}"
fi

# ---- Step 3: Check Python venv ----
if [ ! -d "$VENV_DIR" ]; then
    echo -e "${YELLOW}📦 Creating Python virtual environment...${NC}"
    python3 -m venv "$VENV_DIR"
    echo -e "${CYAN}📦 Installing dependencies...${NC}"
    "$VENV_DIR/bin/pip" install -r "$SERVER_DIR/requirements.txt" --quiet
    echo -e "${GREEN}✅ Dependencies installed${NC}"
fi

# ---- Step 4: Start Server ----
echo ""
echo -e "${CYAN}🚀 Starting Vision Assist server...${NC}"

# Activate venv and start in background
cd "$SERVER_DIR"
"$VENV_DIR/bin/python" server.py > "$LOG_FILE" 2>&1 &
SERVER_PID=$!
echo "$SERVER_PID" > "$PID_FILE"

# Wait a moment and check if it's running
sleep 2
if kill -0 "$SERVER_PID" 2>/dev/null; then
    echo -e "${GREEN}✅ Server started (PID: $SERVER_PID)${NC}"
else
    echo -e "${RED}❌ Server failed to start. Check $LOG_FILE${NC}"
    tail -5 "$LOG_FILE"
    exit 1
fi

# ---- Step 5: Print Connection Info ----
echo ""
echo -e "${PURPLE}${BOLD}═══════════════════════════════════════════${NC}"
echo -e "${BOLD}  📱 Connect from your phone:${NC}"
echo ""
echo -e "  ${CYAN}Phone Mic (HTTPS):${NC}"
echo -e "    ${BOLD}https://$LOCAL_IP:8081/phone${NC}"
echo -e "    ${YELLOW}(Accept the 'Not Secure' warning)${NC}"
echo ""
echo -e "  ${CYAN}Dashboard:${NC}"
echo -e "    ${BOLD}http://$LOCAL_IP:8080${NC}"
echo ""
echo -e "  ${CYAN}ESP32 WebSocket:${NC}"
echo -e "    ${BOLD}ws://$LOCAL_IP:8765${NC}"
echo -e "${PURPLE}${BOLD}═══════════════════════════════════════════${NC}"
echo ""
echo -e "${GREEN}Server running in background. Logs: $LOG_FILE${NC}"
echo -e "${YELLOW}To stop: kill $SERVER_PID  (or run: kill \$(cat $PID_FILE))${NC}"
echo ""

# ---- Optional: Follow logs ----
if [ "$1" == "--follow" ] || [ "$1" == "-f" ]; then
    echo -e "${CYAN}📋 Following server logs (Ctrl+C to stop viewing, server keeps running):${NC}"
    echo ""
    tail -f "$LOG_FILE"
fi
