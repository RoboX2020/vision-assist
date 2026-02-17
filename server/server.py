#!/usr/bin/env python3
"""
Vision Assist — Python Relay Server (Dual-Input)
==================================================
Bridges the ESP32-S3 camera AND the phone's BT headphone mic
to Google's Gemini Live API.

Architecture:
  ESP32  ←→ (WebSocket /ws/esp32)  ←→ This Server ←→ Gemini Live API
  Phone  ←→ (WebSocket /ws/phone)  ←→ This Server ←→   (same session)

  ESP32 sends:  video frames (0x02 + JPEG)
  Phone sends:  mic audio   (0x01 + PCM 16kHz)
  Phone receives: speaker audio (0x03 + PCM 24kHz) → BT headphones

The server:
  1. Accepts WebSocket from ESP32 on /ws/esp32 (camera only)
  2. Accepts WebSocket from Phone on /ws/phone  (mic + speaker)
  3. Opens a single Gemini Live session
  4. Forwards video from ESP32 + audio from Phone to Gemini
  5. Receives audio responses from Gemini → forwards to Phone
  6. Serves web dashboard on port 8080
  7. Serves phone companion page on /phone
"""

import asyncio
import base64
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# Load .env file
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    load_dotenv(env_path)
else:
    print("⚠️  No .env file found. Copy .env.example to .env and add your API key.")

import websockets
import aiohttp
from aiohttp import web
from google import genai
import ssl
from face_engine import FaceEngine
from object_engine import ObjectEngine

# ---- Configuration ----
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash-native-audio-latest"
WS_HOST = "0.0.0.0"
WS_HOST = "0.0.0.0"
WS_PORT = 8765          # Insecure WS (ESP32 only)
HTTP_PORT = 8080        # Insecure HTTP (Dashboard + Phone WS)
HTTPS_PORT = 8081       # Secure HTTPS (Dashboard + Phone WSS)

# SSL Context
SSL_CONTEXT = None
ssl_cert = Path(__file__).parent / "server.crt"
ssl_key = Path(__file__).parent / "server.key"
if ssl_cert.exists() and ssl_key.exists():
    SSL_CONTEXT = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    SSL_CONTEXT.load_cert_chain(certfile=ssl_cert, keyfile=ssl_key)
else:
    print("⚠️  Warning: SSL certs not found. HTTPS/WSS will not be available.")

# ---- Frame Sampling (API optimization) ----
GEMINI_FRAME_INTERVAL_S = 3.0        # Send at most 1 frame per N seconds to Gemini
SCENE_CHANGE_THRESHOLD = 0.05         # 5% size delta = scene change
FORCE_SEND_ON_SCENE_CHANGE = True     # Immediately send if scene changes
STATS_LOG_INTERVAL_S = 30.0           # Log efficiency stats every N seconds

# Protocol prefixes (must match ESP32 config.h and phone.html)
MSG_TYPE_AUDIO_IN = 0x01
MSG_TYPE_VIDEO_IN = 0x02
MSG_TYPE_AUDIO_OUT = 0x03
MSG_TYPE_STATUS = 0x04

def load_medical_profile():
    try:
        profile_path = Path(__file__).parent / "medical_profile.txt"
        if profile_path.exists():
            with open(profile_path, "r") as f:
                return f.read()
    except Exception as e:
        print(f"Error loading medical profile: {e}")
    return "No medical profile available."

MEDICAL_PROFILE = load_medical_profile()

SYSTEM_INSTRUCTION = f"""You are a real-time vision and voice assistant running on a wearable ESP32 camera device.
You can see through the camera and hear through the microphone.

**CORE BEHAVIOR:**
1.  **Health Guardian Mode** 🩺:
    -   **Context**: The user has specific medical vulnerabilities (see below).
    -   **Goal**: Protect their metabolic, cardiovascular, and bone health.
    -   **Triggers to Watch For**:
        -   **BAD**: Junk food, sugary drinks, fried snacks, bakery items, late-night eating. also: Prolonged sitting, slouching, inactivity, screen glare.
        -   **GOOD**: Water, healthy food, walking, stairs, sunlight, deep breathing.
    -   **Action**: 
        -   If you see a **BAD** trigger, IMMEDIATELY but GENTLY interrupt. Remind them of the specific health risk (e.g. "That spikes your liver fat"). Suggest a fix.
        -   If you see a **GOOD** trigger, reinforce it briefly.

    **USER MEDICAL PROFILE**:
    {{MEDICAL_PROFILE}}

2.  **Be Proactive**: If you see something interesting, unexpected, or improved (like a person entering, the user holding an object, or a scene change), **speak up immediately**. Do not wait for the user to ask.
2.  **Visual Triggers**: 
    -   If the user holds up an item, describe it or ask about it.
    -   If a known face appears (from System Updates), greet them by name.
    -   If the environment changes significantly, comment on it.
3.  **Task Guidance**: If guiding a user through a physical task:
    -   Give one step at a time.
    -   **WATCH** the video feed closely.
    -   When you see the step is done (e.g. successful action), **IMMEDIATELY** confirm ("I see you did that") and give the next step.
4.  **Tools**: 
    -   Use `remember_face` to learn people.
    -   Use `locate_object` when asked about lost items."""

# ---- Logging ----
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("VisionAssist")

# ---- Global State ----
class ServerState:
    def __init__(self):
        self.esp32_ws = None          # ESP32 WebSocket (camera)
        self.phone_ws = None          # Phone WebSocket (mic + speaker)
        self.gemini_session = None
        self.esp32_connected = False
        self.phone_connected = False
        self.last_frame = None        # Latest JPEG frame bytes
        self.last_frame_time = 0
        self.frames_received = 0      # Total frames from ESP32
        self.frames_to_gemini = 0     # Frames actually sent to Gemini
        self.frames_skipped = 0       # Frames skipped (sampling/dedup)
        self.audio_chunks_sent = 0
        self.audio_chunks_received = 0
        self.start_time = time.time()
        self.gemini_connected = False
        self.last_transcript = ""
        self.status_log = []
        self.gemini_receive_task = None
        # Frame sampling state
        self.last_gemini_send_time = 0.0
        self.last_gemini_frame_size = 0
        self.last_gemini_frame_size = 0
        self.last_stats_log_time = 0.0
        

        # Face Engine & Object Engine
        self.face_engine = FaceEngine()
        self.last_face_scan_time = 0.0
        self.face_scan_task = None
        
        self.object_engine = ObjectEngine()
        self.object_scan_task = None

    def add_log(self, msg):
        self.status_log.append({"time": time.strftime("%H:%M:%S"), "msg": msg})
        if len(self.status_log) > 50:
            self.status_log = self.status_log[-50:]

    @property
    def connected(self):
        return self.esp32_connected or self.phone_connected

    @property
    def both_connected(self):
        return self.esp32_connected and self.phone_connected

state = ServerState()


# ============================================================
# Gemini Live API Session
# ============================================================
# ---- Tool Definitions ----
TOOLS = [{
    "function_declarations": [{
        "name": "remember_face",
        "description": "Memorize the face currently visible in the camera with a name. Use this when the user says 'This is [Name]' or introduces someone.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "name": {"type": "STRING", "description": "The name of the person."}
            },
            "required": ["name"]
        }
    }]
}, {
    "function_declarations": [{
        "name": "locate_object",
        "description": "Find where an object was last seen. Use this when the user asks 'Where is my [object]?'",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "object_name": {"type": "STRING", "description": "The name of the object to find (e.g. 'keys', 'bottle')."}
            },
            "required": ["object_name"]
        }
    }]
}]

async def start_gemini_session():
    """Open a persistent Gemini Live session."""
    if not GEMINI_API_KEY:
        log.error("No GEMINI_API_KEY set! Add it to .env file.")
        state.add_log("ERROR: No API key configured")
        return None

    client = genai.Client(api_key=GEMINI_API_KEY)

    config = {
        "response_modalities": ["AUDIO"],
        "system_instruction": SYSTEM_INSTRUCTION,
        "tools": TOOLS,
    }

    try:
        # client.aio.live.connect() returns an async context manager
        ctx = client.aio.live.connect(
            model=GEMINI_MODEL,
            config=config,
        )
        session = await ctx.__aenter__()
        state._gemini_ctx = ctx  # save context manager for cleanup
        state.gemini_connected = True
        state.add_log("Gemini Live session opened")
        log.info("Gemini Live session opened")
        return session
    except Exception as e:
        log.error(f"Failed to connect to Gemini: {e}")
        state.add_log(f"ERROR: Gemini connection failed: {e}")
        state.gemini_connected = False
        return None


async def send_audio_to_gemini(session, pcm_data: bytes):
    """Forward PCM audio from phone mic to Gemini."""
    try:
        await session.send_realtime_input(
            audio={"data": pcm_data, "mime_type": "audio/pcm"}
        )
        state.audio_chunks_sent += 1
    except Exception as e:
        log.error(f"Error sending audio to Gemini: {e}")


def should_send_frame_to_gemini(frame_size: int) -> bool:
    """Decide if this frame should be forwarded to Gemini based on timing and scene change."""
    now = time.time()
    elapsed = now - state.last_gemini_send_time

    # Always send if enough time has passed
    if elapsed >= GEMINI_FRAME_INTERVAL_S:
        return True

    # Send early if scene changed significantly
    if FORCE_SEND_ON_SCENE_CHANGE and state.last_gemini_frame_size > 0:
        prev = state.last_gemini_frame_size
        delta = abs(frame_size - prev) / prev
        if delta > SCENE_CHANGE_THRESHOLD and elapsed >= 0.5:
            return True

    return False


async def send_video_to_gemini(session, jpeg_data: bytes):
    """Forward JPEG frame from ESP32 camera to Gemini."""
    try:
        await session.send_realtime_input(
            video={"data": jpeg_data, "mime_type": "image/jpeg"}
        )
        state.frames_to_gemini += 1
        state.last_gemini_send_time = time.time()
        state.last_gemini_frame_size = len(jpeg_data)
    except Exception as e:
        log.error(f"Error sending video to Gemini: {e}")


def maybe_log_stats():
    """Periodically log efficiency stats instead of per-frame spam."""
    now = time.time()
    if now - state.last_stats_log_time >= STATS_LOG_INTERVAL_S:
        state.last_stats_log_time = now
        total = state.frames_received
        sent = state.frames_to_gemini
        skipped = state.frames_skipped
        if total > 0:
            pct = (sent / total) * 100
            log.info(
                f"Frames: {total} received, {sent} sent to Gemini ({pct:.0f}%), "
                f"{skipped} skipped"
            )


async def handle_tool_call(session, tool_call):
    """Execute a tool call from Gemini and send the result back."""
    for fc in tool_call.function_calls:
        name = fc.name
        args = fc.args
        
        if name == "remember_face":
            person_name = args.get("name", "Unknown")
            log.info(f"🧠 Gemini asked to remember face: {person_name}")
            state.add_log(f"🧠 Memorizing face: {person_name}")
            
            # Use the VERY latest frame
            if state.last_frame:
                success = await asyncio.to_thread(state.face_engine.register_face, state.last_frame, person_name)
                result = "Face registered successfully." if success else "Failed to register face (no face found in frame)."
            else:
                result = "Failed: No camera frame available."
            
            state.add_log(result)
            
            # Send result back to Gemini
            await session.send(input=result, end_of_turn=True)

        elif name == "locate_object":
            obj_name = args.get("object_name", "").lower()
            log.info(f"🔍 Gemini asked to locate: {obj_name}")
            state.add_log(f"🔍 Locating: {obj_name}")
            
            # Search memory.json
            found = False
            last_seen = None
            
            if os.path.exists(state.object_engine.db_file):
                try:
                    with open(state.object_engine.db_file, "r") as f:
                        data = json.load(f)
                        # Filter by label (fuzzy match)
                        matches = [d for d in data if obj_name in d["label"].lower()]
                        if matches:
                            last_seen = matches[-1] # Get latest
                            found = True
                except Exception as e:
                    log.error(f"Error reading memory db: {e}")

            if found:
                t_str = last_seen.get("date", "unknown time")
                conf = last_seen.get("confidence", 0)
                result = f"I last saw a '{last_seen['label']}' at {t_str} (Confidence: {conf})."
            else:
                result = f"I haven't seen any object matching '{obj_name}' in my recent memory."
            
            state.add_log(result)
            await session.send(input=result, end_of_turn=True)

async def scan_faces_task(session):
    """Background task to scan for faces periodically."""
    while True:
        try:
            await asyncio.sleep(2.0) # Scan every 2 seconds
            
            if not state.last_frame:
                continue
                
            # Run CPU-bound face processing in thread
            names = await asyncio.to_thread(state.face_engine.process_frame, state.last_frame)
            
            if names:
                known_names = [n for n in names if n != "Unknown"]
                if known_names:
                    # Tell Gemini we see someone
                    msg = f"System Update: The camera sees {', '.join(known_names)}."
                    log.info(f"👀 {msg}")
                    
                    try:
                       await session.send(input=msg, end_of_turn=True)
                    except Exception as e:
                        log.error(f"Error sending face detection to Gemini: {e}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"Error in face scan task: {e}")
            await asyncio.sleep(5)

async def scan_objects_task(session):
    """Background task to scan for objects periodically."""
    while True:
        try:
            await asyncio.sleep(5.0) # Scan every 5 seconds
            
            if not state.last_frame:
                continue
                
            # Run CPU-bound object processing in thread
            labels = await asyncio.to_thread(state.object_engine.process_frame, state.last_frame)
            
            # We don't spam Gemini with objects unless asked, 
            # but we could add logic here for "Visual Triggers" later if needed.
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"Error in object scan task: {e}")
            await asyncio.sleep(5)

async def receive_from_gemini(session):
    """Receive audio responses from Gemini and relay to Phone."""
    try:
        while True:
            turn = session.receive()
            async for response in turn:
                if (response.server_content and
                        response.server_content.model_turn):
                    for part in response.server_content.model_turn.parts:
                        # AUDIO
                        if part.inline_data and isinstance(part.inline_data.data, bytes):
                            audio_data = part.inline_data.data
                            state.audio_chunks_received += 1

                            # Send to Phone BT headphones (not ESP32!)
                            if state.phone_ws:
                                msg = bytes([MSG_TYPE_AUDIO_OUT]) + audio_data
                                try:
                                    # aiohttp WebSocket uses send_bytes
                                    await state.phone_ws.send_bytes(msg)
                                except Exception as e:
                                    log.error(f"Error sending audio to phone: {e}")

                        # TEXT
                        if part.text:
                            state.last_transcript = part.text
                            state.add_log(f"Gemini: {part.text[:100]}")
                            log.info(f"💬 Gemini: {part.text[:100]}")
                
                # TOOL CALLS
                if response.tool_call:
                     await handle_tool_call(session, response.tool_call)

                # Handle interruptions
                if (response.server_content and
                        response.server_content.interrupted):
                    log.info("⚡ Gemini response interrupted")
                    state.add_log("Response interrupted by user")

    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.error(f"Error receiving from Gemini: {e}")
        state.add_log(f"ERROR: Gemini receive error: {e}")


async def ensure_gemini_session():
    """Start Gemini session if not already running. Returns the session."""
    if state.gemini_session:
        return state.gemini_session

    session = await start_gemini_session()
    if not session:
        return None

    state.gemini_session = session
    state.gemini_receive_task = asyncio.create_task(receive_from_gemini(session))
    state.face_scan_task = asyncio.create_task(scan_faces_task(session))
    state.object_scan_task = asyncio.create_task(scan_objects_task(session))
    return session


async def close_gemini_session():
    """Close the Gemini session and cleanup."""
    # Cancel receive task
    if state.gemini_receive_task:
        state.gemini_receive_task.cancel()
        try:
            await state.gemini_receive_task
        except asyncio.CancelledError:
            pass
        state.gemini_receive_task = None
    
    # Cancel face scan task
    if state.face_scan_task:
        state.face_scan_task.cancel()
        try:
            await state.face_scan_task
        except asyncio.CancelledError:
            pass
        state.face_scan_task = None

    # Cancel object scan task
    if state.object_scan_task:
        state.object_scan_task.cancel()
        try:
            await state.object_scan_task
        except asyncio.CancelledError:
            pass
        state.object_scan_task = None

    if state.gemini_session:
        try:
            ctx = getattr(state, '_gemini_ctx', None)
            if ctx:
                await ctx.__aexit__(None, None, None)
                state._gemini_ctx = None
            else:
                await state.gemini_session.close()
        except Exception:
            pass
        state.gemini_session = None
        state.gemini_connected = False


# ============================================================
# ESP32 WebSocket Handler (Camera Only)
# ============================================================
async def handle_esp32(websocket):
    """Handle WebSocket connection from ESP32 — video frames only."""
    state.esp32_ws = websocket
    state.esp32_connected = True
    client_ip = websocket.remote_address[0] if websocket.remote_address else "unknown"
    log.info(f"📷 ESP32 camera connected from {client_ip}")
    state.add_log(f"ESP32 camera connected from {client_ip}")

    # Send welcome
    welcome = bytes([MSG_TYPE_STATUS]) + b"Connected - camera streaming mode"
    await websocket.send(welcome)

    # Start Gemini if phone is already connected
    session = await ensure_gemini_session()
    if not session:
        error_msg = bytes([MSG_TYPE_STATUS]) + b"ERROR: Gemini API not available"
        await websocket.send(error_msg)

    # Notify phone
    if state.phone_ws:
        try:
            status = bytes([MSG_TYPE_STATUS]) + b"ESP32 camera connected"
            await state.phone_ws.send(status)
        except Exception:
            pass

    try:
        async for message in websocket:
            if isinstance(message, bytes) and len(message) > 1:
                msg_type = message[0]
                payload = message[1:]

                if msg_type == MSG_TYPE_VIDEO_IN:
                    # Always store latest frame for dashboard
                    state.last_frame = payload
                    state.frames_received += 1
                    state.last_frame_time = time.time()

                    # Smart sampling: only send to Gemini if needed
                    if state.gemini_session and should_send_frame_to_gemini(len(payload)):
                        await send_video_to_gemini(state.gemini_session, payload)
                    else:
                        state.frames_skipped += 1

                    maybe_log_stats()

    except websockets.exceptions.ConnectionClosed as e:
        log.info(f"ESP32 disconnected: {e}")
        state.add_log("ESP32 camera disconnected")
    except Exception as e:
        log.error(f"ESP32 handler error: {e}")
        state.add_log(f"ERROR: {e}")
    finally:
        state.esp32_ws = None
        state.esp32_connected = False

        # Only close Gemini if phone is also disconnected
        if not state.phone_connected:
            await close_gemini_session()

        log.info("ESP32 session cleaned up")


# ============================================================
# Phone WebSocket Handler (Audio I/O via BT Headphones)
# ============================================================
async def handle_phone_ws(request):
    """Handle Phone WebSocket connection via aiohttp (Same Port)."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    state.phone_connected = True
    state.phone_ws = ws
    state.add_log("📱 Phone connected via aiohttp WS")
    log.info("📱 Phone connected via aiohttp WS")

    # Send welcome
    welcome = bytes([MSG_TYPE_STATUS]) + b"Connected - audio via BT headphones"
    try:
        await ws.send_bytes(welcome)
    except Exception:
        pass

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                data = msg.data
                if len(data) < 1:
                    continue

                msg_type = data[0]
                payload = data[1:]

                if msg_type == MSG_TYPE_AUDIO_IN:
                    # Audio from Phone Mic
                    if state.gemini_session:
                        await send_audio_to_gemini(state.gemini_session, payload)
                
            elif msg.type == aiohttp.WSMsgType.ERROR:
                log.error(f"Phone WS connection closed with exception {ws.exception()}")

    finally:
        state.phone_connected = False
        state.phone_ws = None
        state.add_log("Phone disconnected")
        log.info("Phone disconnected")
    
    return ws


# ============================================================
# WebSocket Router — dispatch by path
# ============================================================
async def ws_router(websocket):
    """Route WebSocket connections based on URL path."""
    path = websocket.request.path if hasattr(websocket, 'request') else ""

    if path == "/ws/esp32":
        await handle_esp32(websocket)
    elif path == "/ws/phone":
        await handle_phone(websocket)
    else:
        # Legacy: treat as ESP32 for backward compatibility
        log.warning(f"Unknown WS path '{path}', treating as ESP32")
        await handle_esp32(websocket)


async def ws_server():
    """Run the WebSocket server for both ESP32 and Phone connections."""
    # 1. Insecure WS (Port 8765) - For ESP32 (Legacy / Hardware)
    log.info(f"🌐 WS (Insecure) listening on ws://{WS_HOST}:{WS_PORT}")
    # We only serve ESP32 on this dedicated port now
    await websockets.serve(
        handle_esp32,  # Direct handler for ESP32
        WS_HOST,
        WS_PORT,
        max_size=1024 * 1024,
        ping_interval=20,
        ping_timeout=10,
    )
    
    state.add_log(f"ESP32 WS server started on {WS_PORT}")
    await asyncio.Future()  # Run forever


# ============================================================
# Web Dashboard (HTTP)
# ============================================================
async def handle_dashboard(request):
    """Serve the main dashboard page."""
    dashboard_path = Path(__file__).parent / "dashboard.html"
    if dashboard_path.exists():
        return web.FileResponse(dashboard_path)
    return web.Response(text="Dashboard not found", status=404)


async def handle_phone_page(request):
    """Serve the phone companion page."""
    phone_path = Path(__file__).parent / "phone.html"
    if phone_path.exists():
        return web.FileResponse(phone_path)
    return web.Response(text="Phone page not found", status=404)


async def handle_status_api(request):
    """REST API for dashboard status updates."""
    uptime = int(time.time() - state.start_time)
    efficiency = 0
    if state.frames_received > 0:
        efficiency = round((1 - state.frames_to_gemini / state.frames_received) * 100)
    data = {
        "esp32_connected": state.esp32_connected,
        "phone_connected": state.phone_connected,
        "gemini_connected": state.gemini_connected,
        "frames_received": state.frames_received,
        "frames_to_gemini": state.frames_to_gemini,
        "frames_skipped": state.frames_skipped,
        "api_savings_pct": efficiency,
        "audio_chunks_sent": state.audio_chunks_sent,
        "audio_chunks_received": state.audio_chunks_received,
        "last_transcript": state.last_transcript,
        "uptime_seconds": uptime,
        "logs": state.status_log[-20:],
    }
    return web.json_response(data)


async def handle_frame_api(request):
    """Serve the latest camera frame as JPEG."""
    if state.last_frame:
        return web.Response(
            body=state.last_frame,
            content_type="image/jpeg",
            headers={"Cache-Control": "no-cache"},
        )
    return web.Response(status=204)


async def http_server():
    """Run the HTTP dashboard server (Dual Stack)."""
    app = web.Application()
    app.router.add_get("/", handle_dashboard)
    app.router.add_get("/phone", handle_phone_page)
    # Mount WebSocket handler directly on aiohttp
    app.router.add_get("/ws/phone", handle_phone_ws)
    
    app.router.add_get("/api/status", handle_status_api)
    app.router.add_get("/api/frame", handle_frame_api)

    runner = web.AppRunner(app)
    await runner.setup()

    # 1. Insecure HTTP (Port 8080)
    site_http = web.TCPSite(runner, "0.0.0.0", HTTP_PORT)
    await site_http.start()
    log.info(f"📊 Dashboard (HTTP):  http://localhost:{HTTP_PORT}")
    
    # 2. Secure HTTPS (Port 8081)
    if SSL_CONTEXT:
        site_https = web.TCPSite(runner, "0.0.0.0", HTTPS_PORT, ssl_context=SSL_CONTEXT)
        await site_https.start()
        log.info(f"🔒 Dashboard (HTTPS): https://localhost:{HTTPS_PORT}")

    state.add_log(f"HTTP servers started on {HTTP_PORT} and {HTTPS_PORT}")


# ============================================================
# Main
# ============================================================
async def main():
    """Start all server components."""
    print()
    print("=" * 50)
    print("  Vision Assist — Relay Server")
    print("  ESP32 Camera + Phone BT Headphones")
    print("=" * 50)
    print()

    if not GEMINI_API_KEY:
        print("⚠️  WARNING: GEMINI_API_KEY not set!")
        print("   Copy .env.example to .env and add your key.")
        print("   Or set it: export GEMINI_API_KEY=your_key")
        print()

    # Get local IP for ESP32 config
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "127.0.0.1"

    print(f"📡 Your server IP: {local_ip}")
    print()
    print(f"   ESP32 config.h → SERVER_HOST: {local_ip}")
    print(f"   ESP32 WebSocket: ws://{local_ip}:{WS_PORT}/ws/esp32")
    print()
    print("   📱 Phone Companion (Mic Access):")
    print(f"      HTTPS: https://{local_ip}:{HTTPS_PORT}/phone")
    print("      (Accept 'Not Secure' warning to enable Mic)")
    print()
    print(f"   📊 Dashboard: http://{local_ip}:{HTTP_PORT}")
    print()
    print("How it works:")
    print("  1. ESP32 sends camera frames via WiFi")
    print("  2. Phone sends mic audio from BT headphones")
    print("  3. Gemini sees + hears → responds via BT headphones")
    print()
    print("Waiting for connections...")
    print()

    # Start HTTP dashboard
    await http_server()

    # Start WebSocket server (runs forever)
    await ws_server()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Server stopped.")
        sys.exit(0)
