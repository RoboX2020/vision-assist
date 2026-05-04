#!/bin/bash
# ============================================================
# Vision Assist — Cloud/RPi Setup Script
# ============================================================
# Run this on a fresh Ubuntu/Debian/Raspberry Pi to set up
# the Vision Assist server for headless (no-Mac) operation.
#
# Usage: curl -sSL <url> | bash
#   or:  chmod +x setup_remote.sh && ./setup_remote.sh
# ============================================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
PURPLE='\033[0;35m'
NC='\033[0m'
BOLD='\033[1m'

echo ""
echo -e "${PURPLE}${BOLD}╔══════════════════════════════════════════════╗${NC}"
echo -e "${PURPLE}${BOLD}║  🔮 Vision Assist — Remote Setup Script      ║${NC}"
echo -e "${PURPLE}${BOLD}╚══════════════════════════════════════════════╝${NC}"
echo ""

INSTALL_DIR="${INSTALL_DIR:-$HOME/vision_assist}"

# ---- Step 1: System Dependencies ----
echo -e "${CYAN}📦 Installing system dependencies...${NC}"
if command -v apt-get &> /dev/null; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq python3 python3-pip python3-venv \
        build-essential cmake libssl-dev \
        libopenblas-dev liblapack-dev \
        libjpeg-dev libpng-dev \
        2>/dev/null
    echo -e "${GREEN}✅ System packages installed${NC}"
elif command -v brew &> /dev/null; then
    echo -e "${YELLOW}macOS detected, using brew${NC}"
    brew install python3 cmake 2>/dev/null || true
else
    echo -e "${RED}❌ Unsupported package manager. Install Python 3.10+ manually.${NC}"
    exit 1
fi

# ---- Step 2: Project Directory ----
echo ""
echo -e "${CYAN}📁 Setting up project in: $INSTALL_DIR${NC}"
mkdir -p "$INSTALL_DIR/server"
mkdir -p "$INSTALL_DIR/esp32_firmware"

# ---- Step 3: Python Virtual Environment ----
echo -e "${CYAN}🐍 Creating Python virtual environment...${NC}"
cd "$INSTALL_DIR/server"

if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
source venv/bin/activate

# ---- Step 4: Install Python Dependencies ----
echo -e "${CYAN}📦 Installing Python dependencies...${NC}"
pip install --upgrade pip --quiet
pip install \
    "google-genai>=1.0.0" \
    "websockets>=12.0" \
    "python-dotenv>=1.0.0" \
    "aiohttp>=3.9.0" \
    "opencv-python-headless>=4.8.0" \
    "numpy>=1.24.0" \
    "ultralytics>=8.0.0" \
    --quiet 2>/dev/null

# Note: face_recognition requires dlib which is heavy. 
# On RPi, use opencv-python-headless instead of full opencv
echo -e "${YELLOW}⚠️  face_recognition requires dlib (heavy compile). Installing separately...${NC}"
pip install face_recognition --quiet 2>/dev/null || {
    echo -e "${YELLOW}⚠️  face_recognition install failed. Face recognition will be disabled.${NC}"
    echo -e "${YELLOW}   This is OK — all other features work fine.${NC}"
}

echo -e "${GREEN}✅ Python dependencies installed${NC}"

# ---- Step 5: Generate SSL Certificates ----
echo ""
echo -e "${CYAN}🔒 Generating self-signed SSL certificate for HTTPS...${NC}"
if [ ! -f "server.crt" ]; then
    openssl req -x509 -newkey rsa:2048 -keyout server.key -out server.crt \
        -days 3650 -nodes -subj "/CN=VisionAssist" 2>/dev/null
    echo -e "${GREEN}✅ SSL certificates generated (valid for 10 years)${NC}"
else
    echo -e "${GREEN}✅ SSL certificates already exist${NC}"
fi

# ---- Step 6: Environment File ----
if [ ! -f ".env" ]; then
    echo -e "${YELLOW}📋 Creating .env file — you need to add your Gemini API key!${NC}"
    cat > .env << 'EOF'
# Vision Assist — Environment Variables
GEMINI_API_KEY=your_api_key_here
EOF
    echo -e "${RED}⚠️  IMPORTANT: Edit .env and add your GEMINI_API_KEY!${NC}"
    echo -e "   nano $INSTALL_DIR/server/.env"
else
    echo -e "${GREEN}✅ .env file exists${NC}"
fi

# ---- Step 7: Systemd Service (Linux only) ----
if command -v systemctl &> /dev/null; then
    echo ""
    echo -e "${CYAN}🔧 Creating systemd service for auto-start on boot...${NC}"
    
    SERVICE_FILE="/etc/systemd/system/vision-assist.service"
    sudo tee "$SERVICE_FILE" > /dev/null << EOF
[Unit]
Description=Vision Assist Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$INSTALL_DIR/server
ExecStart=$INSTALL_DIR/server/venv/bin/python server.py
Restart=always
RestartSec=10
Environment=PATH=$INSTALL_DIR/server/venv/bin:/usr/bin:/bin

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    echo -e "${GREEN}✅ Systemd service created${NC}"
    echo -e "   ${CYAN}To start:${NC}  sudo systemctl start vision-assist"
    echo -e "   ${CYAN}To enable:${NC} sudo systemctl enable vision-assist"
    echo -e "   ${CYAN}To check:${NC}  sudo systemctl status vision-assist"
    echo -e "   ${CYAN}Logs:${NC}     journalctl -u vision-assist -f"
fi

# ---- Done ----
echo ""
echo -e "${PURPLE}${BOLD}═══════════════════════════════════════════════${NC}"
echo -e "${GREEN}${BOLD}  ✅ Setup Complete!${NC}"
echo ""
echo -e "  ${BOLD}Next steps:${NC}"
echo -e "  1. Add your Gemini API key:  ${CYAN}nano $INSTALL_DIR/server/.env${NC}"
echo -e "  2. Copy server files from Mac: ${CYAN}scp -r server.py *.py *.html *.json *.txt user@<ip>:$INSTALL_DIR/server/${NC}"
echo -e "  3. Start the server:          ${CYAN}cd $INSTALL_DIR && ./start.sh${NC}"
echo ""
echo -e "  ${BOLD}For Raspberry Pi:${NC}"
echo -e "  • Connect to phone hotspot WiFi"
echo -e "  • Run: sudo systemctl start vision-assist"
echo -e "  • The server auto-starts on boot with: sudo systemctl enable vision-assist"
echo -e "${PURPLE}${BOLD}═══════════════════════════════════════════════${NC}"
echo ""
