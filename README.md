# Vision Assist — ESP32-S3 Camera + Phone BT Headphones + Gemini Live API

A real-time vision and voice assistant using:
- **Camera**: Seeed Studio XIAO ESP32S3 Sense (OV2640)
- **Audio**: Your phone's Bluetooth headphones (mic + speaker)
- **Server**: Python relay on Mac/PC
- **AI**: Google Gemini Live API (Multimodal: Audio+Video → Audio)

## Architecture

```
ESP32 Camera ──WiFi──→ ┌─────────────┐ ──→ Gemini Live API
                       │ Relay Server │
Phone BT Mic ──WiFi──→ │  (Mac/PC)   │ ←── Audio responses
                       └─────────────┘ ──→ Phone BT Headphones
```

## 1. Hardware Setup

### Components
- **Seeed Studio XIAO ESP32S3 Sense** (with expansion board for camera)
- **USB-C Cable** (for power & programming)
- **Bluetooth Headphones** (connected to your phone)

> No external mic or speaker modules needed! Audio goes through your BT headphones.

## 2. Server Setup (Mac/PC)

1.  **Install Python requirements**:
    ```bash
    cd server
    pip install -r requirements.txt
    ```

2.  **Configure API Key**:
    ```bash
    cp .env.example .env
    nano .env  # Add GEMINI_API_KEY=...
    ```
    Get a free key at: https://aistudio.google.com/apikey

3.  **Run the Server**:
    ```bash
    python server.py
    ```
    The server prints:
    - **Server IP** — needed for ESP32 config
    - **Phone URL** — open on your phone: `http://<SERVER_IP>:8080/phone`
    - **Dashboard** — view on Mac: `http://<SERVER_IP>:8080`

## 3. Firmware Setup (ESP32)

1.  **Open** `esp32_firmware/esp32_firmware.ino` in Arduino IDE.

2.  **Edit** `esp32_firmware/config.h`:
    - `WIFI_SSID` — Your phone hotspot name
    - `WIFI_PASSWORD` — Hotspot password
    - `SERVER_HOST` — IP printed by the server

3.  **Install Libraries**:
    - Board: **Seeed Studio XIAO ESP32S3**
    - Library: `WebSockets` by Markus Sattler

4.  **Flash** via USB.

## 4. Usage

1.  **Connect BT headphones** to your phone.
2.  **Turn on phone hotspot** — ESP32 and Mac connect to it.
3.  **Start the server** on your Mac.
4.  **Power ESP32** — blue LED turns on when connected.
5.  **Open phone page** — `http://<SERVER_IP>:8080/phone` in your phone browser.
6.  **Tap the mic button** — start talking! Gemini sees through the camera and responds via your headphones.

### Tips
- Phone screen can be locked — hotspot stays active in background.
- The phone page has a **Wake Lock** to prevent sleep while active.
- Works with any BT headphones (AirPods, Galaxy Buds, etc.).
