// ============================================================
// Vision Assist — Configuration
// Edit these values to match your hardware and network setup
// ============================================================

#ifndef CONFIG_H
#define CONFIG_H

// ---- WiFi (Phone Hotspot) ----
#define WIFI_SSID       "Gogo"
#define WIFI_PASSWORD   "12345678"

// ---- Relay Server ----
// IP address of the Mac running server.py (on the same hotspot network)
// Find it with: ifconfig | grep "inet " on your Mac
#define SERVER_HOST     "172.20.10.11"
#define SERVER_PORT     8765
#define SERVER_PATH     "/ws/esp32"

// ---- Camera (OV2640) ----
// XIAO ESP32S3 Sense camera pins (do NOT change unless different board)
#define CAM_PIN_PWDN    -1
#define CAM_PIN_RESET   -1
#define CAM_PIN_XCLK    10
#define CAM_PIN_SIOD    40
#define CAM_PIN_SIOC    39
#define CAM_PIN_D7      48
#define CAM_PIN_D6      11
#define CAM_PIN_D5      12
#define CAM_PIN_D4      14
#define CAM_PIN_D3      16
#define CAM_PIN_D2      18
#define CAM_PIN_D1      17
#define CAM_PIN_D0      15
#define CAM_PIN_VSYNC   38
#define CAM_PIN_HREF    47
#define CAM_PIN_PCLK    13

// Camera settings
#define CAMERA_FRAME_SIZE   FRAMESIZE_QVGA   // 320x240 -- good balance of quality & bandwidth
#define CAMERA_JPEG_QUALITY 30               // 0-63, lower = better quality (30 = fast/smooth)
#define CAMERA_FB_COUNT     2                // Frame buffer count
#define CAMERA_CAPTURE_INTERVAL_MS 66        // 15 FPS local capture (server controls Gemini rate)

// ---- Protocol Prefixes ----
// Binary message format: [1-byte type prefix] + [payload]
#define MSG_TYPE_VIDEO_IN   0x02  // ESP32 → Server: camera JPEG frame
#define MSG_TYPE_STATUS     0x04  // Server → ESP32: status/command messages

// ---- Status LED ----
#define LED_PIN         21   // Built-in LED on XIAO ESP32S3

// ---- Misc ----
#define WEBSOCKET_RECONNECT_INTERVAL_MS 3000
#define WIFI_CONNECT_TIMEOUT_MS         15000

#endif // CONFIG_H
