// ============================================================
// Vision Assist — ESP32-S3 Sense Firmware (Camera Only)
// Real-time vision assistant using Gemini Live API
//
// Hardware: Seeed Studio XIAO ESP32S3 Sense
//   - OV2640 camera  → captures JPEG frames
//   - WiFi           → connects to phone hotspot
//
// Audio I/O is handled by the phone via Bluetooth headphones.
// The ESP32 only sends camera frames to the relay server.
//
// Protocol: WebSocket binary messages to Python relay server
//   0x02 + JPEG data  → camera frame to server
//   0x04 + text       ← status message from server
// ============================================================

#include <WiFi.h>
#include <WebSocketsClient.h>
#include "esp_camera.h"
#include "config.h"

// ---- Global Objects ----
WebSocketsClient webSocket;

// ---- State ----
volatile bool wsConnected = false;
unsigned long lastFrameTime = 0;
unsigned long lastWifiCheck = 0;

// ============================================================
// LED helpers
// ============================================================
void ledOn()  { digitalWrite(LED_PIN, LOW);  }  // active-low on XIAO
void ledOff() { digitalWrite(LED_PIN, HIGH); }

void ledBlink(int times, int ms) {
  for (int i = 0; i < times; i++) {
    ledOn(); delay(ms);
    ledOff(); delay(ms);
  }
}

// ============================================================
// WiFi
// ============================================================
bool connectWiFi() {
  Serial.printf("[WiFi] Connecting to %s ...\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED) {
    if (millis() - start > WIFI_CONNECT_TIMEOUT_MS) {
      Serial.println("[WiFi] Connection timeout!");
      return false;
    }
    delay(500);
    Serial.print(".");
  }

  Serial.printf("\n[WiFi] Connected! IP: %s\n", WiFi.localIP().toString().c_str());
  return true;
}

// ============================================================
// Camera (OV2640)
// ============================================================
bool initCamera() {
  camera_config_t config;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer   = LEDC_TIMER_0;
  config.pin_d0       = CAM_PIN_D0;
  config.pin_d1       = CAM_PIN_D1;
  config.pin_d2       = CAM_PIN_D2;
  config.pin_d3       = CAM_PIN_D3;
  config.pin_d4       = CAM_PIN_D4;
  config.pin_d5       = CAM_PIN_D5;
  config.pin_d6       = CAM_PIN_D6;
  config.pin_d7       = CAM_PIN_D7;
  config.pin_xclk     = CAM_PIN_XCLK;
  config.pin_pclk     = CAM_PIN_PCLK;
  config.pin_vsync    = CAM_PIN_VSYNC;
  config.pin_href     = CAM_PIN_HREF;
  config.pin_sccb_sda = CAM_PIN_SIOD;
  config.pin_sccb_scl = CAM_PIN_SIOC;
  config.pin_pwdn     = CAM_PIN_PWDN;
  config.pin_reset    = CAM_PIN_RESET;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;
  config.frame_size   = CAMERA_FRAME_SIZE;
  config.jpeg_quality = CAMERA_JPEG_QUALITY;
  config.fb_count     = CAMERA_FB_COUNT;
  config.fb_location  = CAMERA_FB_IN_PSRAM;
  config.grab_mode    = CAMERA_GRAB_LATEST;

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("[Camera] Init failed: 0x%x\n", err);
    return false;
  }

  // Adjust sensor settings for better indoor performance
  sensor_t *s = esp_camera_sensor_get();
  if (s) {
    s->set_brightness(s, 1);     // slightly brighter
    s->set_saturation(s, 0);
    s->set_whitebal(s, 1);       // auto white balance
    s->set_awb_gain(s, 1);
    s->set_exposure_ctrl(s, 1);  // auto exposure
    s->set_aec2(s, 1);           // auto exposure DSP
    s->set_gain_ctrl(s, 1);      // auto gain
  }

  Serial.println("[Camera] Initialized OK");
  return true;
}

void captureAndSendFrame() {
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) {
    Serial.println("[Camera] Capture failed");
    return;
  }

  if (fb->format != PIXFORMAT_JPEG) {
    Serial.println("[Camera] Not JPEG format");
    esp_camera_fb_return(fb);
    return;
  }

  // Build message: [0x02] + [JPEG bytes]
  size_t msgLen = 1 + fb->len;
  uint8_t *msg = (uint8_t *)heap_caps_malloc(msgLen, MALLOC_CAP_SPIRAM);
  if (!msg) {
     msg = (uint8_t *)malloc(msgLen); // Fallback
  }
  
  if (msg) {
    msg[0] = MSG_TYPE_VIDEO_IN;
    memcpy(msg + 1, fb->buf, fb->len);
    webSocket.sendBIN(msg, msgLen);
    // Serial.printf("[Camera] Sent frame: %u bytes\n", fb->len); // Reduce noise
    free(msg);
  }

  esp_camera_fb_return(fb);
}

// ============================================================
// WebSocket
// ============================================================
void webSocketEvent(WStype_t type, uint8_t *payload, size_t length) {
  switch (type) {
    case WStype_DISCONNECTED:
      wsConnected = false;
      Serial.println("[WS] Disconnected");
      ledBlink(3, 200);
      break;

    case WStype_CONNECTED:
      wsConnected = true;
      Serial.printf("[WS] Connected to %s\n", (char *)payload);
      ledOn();
      break;

    case WStype_BIN:
      if (length > 1) {
        uint8_t msgType = payload[0];
        if (msgType == MSG_TYPE_STATUS) {
          // Status message from server
          char statusMsg[256];
          size_t copyLen = min(length - 1, (size_t)255);
          memcpy(statusMsg, payload + 1, copyLen);
          statusMsg[copyLen] = '\0';
          Serial.printf("[Server] %s\n", statusMsg);
        }
      }
      break;

    case WStype_TEXT:
      Serial.printf("[WS] Text: %s\n", (char *)payload);
      break;

    case WStype_ERROR:
      Serial.println("[WS] Error!");
      break;

    case WStype_PING:
    case WStype_PONG:
      break;
  }
}

void connectWebSocket() {
  Serial.printf("[WS] Connecting to %s:%d%s\n", SERVER_HOST, SERVER_PORT, SERVER_PATH);
  webSocket.begin(SERVER_HOST, SERVER_PORT, SERVER_PATH);
  webSocket.onEvent(webSocketEvent);
  webSocket.setReconnectInterval(WEBSOCKET_RECONNECT_INTERVAL_MS);
  webSocket.enableHeartbeat(15000, 3000, 2);  // ping every 15s
}

// ============================================================
// Setup
// ============================================================
void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("\n========================================");
  Serial.println("  Vision Assist — ESP32-S3 Camera");
  Serial.println("  Audio via Phone BT Headphones");
  Serial.println("========================================\n");

  // LED
  pinMode(LED_PIN, OUTPUT);
  ledOff();

  // WiFi
  if (!connectWiFi()) {
    Serial.println("[FATAL] WiFi failed. Restarting in 5s...");
    ledBlink(10, 100);
    delay(5000);
    ESP.restart();
  }
  ledBlink(2, 300);

  // Camera
  if (!initCamera()) {
    Serial.println("[FATAL] Camera init failed. Restarting in 5s...");
    ledBlink(10, 100);
    delay(5000);
    ESP.restart();
  }

  // WebSocket
  connectWebSocket();

  Serial.println("\n[Ready] Camera initialized!");
  Serial.println("[Ready] Audio handled by phone BT headphones.");
  Serial.println("[Ready] Waiting for server connection...\n");
}

// ============================================================
// Main Loop
// ============================================================
void loop() {
  // Handle WebSocket events
  webSocket.loop();

  // Check WiFi health
  if (millis() - lastWifiCheck > 5000) {
    lastWifiCheck = millis();
    if (WiFi.status() != WL_CONNECTED) {
      Serial.println("[WiFi] Lost connection, reconnecting...");
      ledOff();
      connectWiFi();
      connectWebSocket();
    }
  }

  if (wsConnected) {
    // Capture and send camera frame at configured interval
    if (millis() - lastFrameTime >= CAMERA_CAPTURE_INTERVAL_MS) {
      lastFrameTime = millis();
      captureAndSendFrame();
    }
  }

  // Small yield to prevent watchdog reset
  delay(1);
}
