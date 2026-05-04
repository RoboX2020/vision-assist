# ============================================================
# Vision Assist — Relay Server
# ============================================================
# Responsibilities:
#   1. Receive video from ESP32 via WebSocket  (/ws/esp32)
#   2. Receive audio from Phone via WebSocket  (/ws/phone)
#   3. Relay both to the Google Gemini Live API
#   4. Forward Gemini audio responses to the phone (BT headphones)
#   5. Run background face recognition and inject names into Gemini

import asyncio
import hashlib
import hmac
import logging
import os
import re
import signal
import ssl
import sys
import time
from pathlib import Path

import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from google import genai
from google.genai import types

from face_engine import FaceEngine
from memory_engine import MemoryEngine

# ---- Environment ----
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    load_dotenv(env_path)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL   = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-native-audio-latest")
HTTP_PORT      = int(os.environ.get("PORT", 8080))
HTTPS_PORT     = int(os.environ.get("HTTPS_PORT", 8443))
AUTH_TOKEN     = os.environ.get("AUTH_TOKEN", "")
DATA_DIR       = Path(os.environ.get("DATA_DIR", Path(__file__).parent))

# Optional medical profile (only surfaced if user asks about food/health)
MEDICAL_PROFILE = ""
for _p in (DATA_DIR / "medical_profile.txt", Path(__file__).parent / "medical_profile.txt"):
    try:
        if _p.exists():
            MEDICAL_PROFILE = _p.read_text(encoding="utf-8").strip()
            break
    except Exception:
        pass

# SSL (only for self-hosted; cloud providers terminate TLS at the edge)
SSL_CONTEXT = None
_cert = Path(__file__).parent / "server.crt"
_key  = Path(__file__).parent / "server.key"
if _cert.exists() and _key.exists():
    SSL_CONTEXT = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    SSL_CONTEXT.load_cert_chain(certfile=_cert, keyfile=_key)

# ---- Tuning ----
GEMINI_FRAME_INTERVAL_S     = 0.5     # ~2 FPS to Gemini
IDLE_TIMEOUT_S              = 300.0   # 5 min idle → close Gemini session
GEMINI_RECONNECT_BASE_S     = 2.0
GEMINI_RECONNECT_MAX_S      = 60.0
FACE_RECOGNITION_INTERVAL_S = 2.5     # min gap between face passes
FACE_FORGET_AFTER_S         = 90.0    # forget "already greeted" after this gap
WS_MAX_FRAME_BYTES          = 256 * 1024   # cap a single ESP32 frame
WS_MAX_AUDIO_BYTES          = 64 * 1024    # cap a single phone audio chunk

# Protocol prefixes
MSG_TYPE_AUDIO_IN  = 0x01
MSG_TYPE_VIDEO_IN  = 0x02
MSG_TYPE_AUDIO_OUT = 0x03
MSG_TYPE_STATUS    = 0x04


# ============================================================
# Auth
# ============================================================

def _check_token(token: str) -> bool:
    """Constant-time token compare. Empty AUTH_TOKEN = open (dev only)."""
    if not AUTH_TOKEN:
        return True
    if not token:
        return False
    return hmac.compare_digest(token, AUTH_TOKEN)


def _token_from_request(request: web.Request) -> str:
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[7:]
    return request.rel_url.query.get("token", "")


async def _require_auth(request: web.Request):
    if not _check_token(_token_from_request(request)):
        return web.Response(status=401, text="Unauthorized")
    return None


# ============================================================
# Logging
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("VisionAssist")


# ============================================================
# System prompt
# ============================================================

def build_system_instruction(memory: "MemoryEngine") -> str:
    """Compact prompt that nudges natural, companion-like behavior."""
    profile   = memory.get_profile_summary()
    ai_name   = memory.profile.get("ai_name") or "Buddy"
    user_name = memory.profile.get("user_name") or ""

    name_line = f"Their name is {user_name}." if user_name else ""
    medical   = (
        "If — and only if — they ask about food, drink, or a health choice, "
        "you may use this medical context:\n" + MEDICAL_PROFILE
        if MEDICAL_PROFILE else ""
    )

    return f"""You are {ai_name}, a voice + vision companion living on smart glasses.
You see what they see, hear what they hear, in real time. {name_line}

Sound like a person, not a product. Short replies. One or two lines is usually right.
Don't narrate the video unless they ask, or unless something matters (a hazard, someone walking up, a missing item).
No bullet points. No "I'd be happy to help." No filler.
If you've already said it, drop it.
If you recognize a face from a System Update, say hi by name like a normal human would — once, not every time you see them.
For step-by-step guidance: one step, watch, confirm, next step.
Use tools (remember_face, save_user_preference, recall_user_info) silently when it fits — never announce them.
If they tell you their name, a fact about themselves, or rename you — just save it and keep talking.

What you already know:
{profile}

{medical}""".strip()


# ============================================================
# Server state
# ============================================================

class ServerState:
    def __init__(self):
        self.esp32_ws         = None
        self.phone_ws         = None
        self.gemini_session   = None
        self.esp32_connected  = False
        self.phone_connected  = False
        self.gemini_connected = False

        self.last_frame: bytes | None = None
        self.last_frame_hash: str = ""
        self.last_frame_time = 0.0
        self.last_transcript = ""
        self.status_log: list[dict] = []
        self.start_time = time.time()

        # Background tasks
        self.gemini_receive_task = None
        self.idle_check_task     = None
        self.face_task           = None
        self.flush_task          = None
        self._gemini_ctx         = None

        # Frame sampling / activity
        self.last_gemini_send_time = 0.0
        self.last_activity_time    = time.time()

        # Reconnect guard
        self.gemini_reconnect_attempts = 0
        self._reconnect_lock          = asyncio.Lock()
        self._reconnect_in_progress   = False
        self._auth_failed             = False  # if true, don't auto-reconnect

        # Engines
        self.face_engine = FaceEngine(data_dir=DATA_DIR)
        self.memory      = MemoryEngine(profile_path=DATA_DIR / "user_profile.json")

        # Counters
        self.frames_received       = 0
        self.frames_to_gemini      = 0
        self.audio_chunks_sent     = 0
        self.audio_chunks_received = 0

    def add_log(self, msg: str):
        self.status_log.append({"time": time.strftime("%H:%M:%S"), "msg": msg})
        if len(self.status_log) > 50:
            self.status_log = self.status_log[-50:]

    @property
    def connected(self) -> bool:
        return self.esp32_connected or self.phone_connected


state = ServerState()


# ============================================================
# Gemini Live API
# ============================================================

TOOLS = [
    {
        "function_declarations": [{
            "name": "remember_face",
            "description": "Memorize the face currently visible in the camera with a name.",
            "parameters": {
                "type": "OBJECT",
                "properties": {"name": {"type": "STRING", "description": "The person's name."}},
                "required": ["name"],
            },
        }]
    },
    {
        "function_declarations": [{
            "name": "save_user_preference",
            "description": "Save something about the user — name, preference, mood, fact, or rename yourself.",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "key":   {"type": "STRING", "description": "Category (name, ai_name, mood, like, dislike, remember, ...)."},
                    "value": {"type": "STRING", "description": "The value to save."},
                },
                "required": ["key", "value"],
            },
        }]
    },
    {
        "function_declarations": [{
            "name": "recall_user_info",
            "description": "Recall saved info about the user.",
            "parameters": {
                "type": "OBJECT",
                "properties": {"key": {"type": "STRING", "description": "Category to look up, or 'all' for everything."}},
                "required": ["key"],
            },
        }]
    },
]


def _safe_arg(args, key: str, limit: int = 200) -> str:
    """Pull a string arg from a Gemini function call, defending against None/types."""
    if not args:
        return ""
    val = args.get(key) if isinstance(args, dict) else None
    if val is None:
        return ""
    if not isinstance(val, str):
        val = str(val)
    return val.strip()[:limit]


async def start_gemini_session():
    if not GEMINI_API_KEY:
        log.error("GEMINI_API_KEY not set — cannot connect to Gemini")
        return None
    if state._auth_failed:
        return None

    client = genai.Client(api_key=GEMINI_API_KEY)
    config = {
        "response_modalities": ["AUDIO"],
        "system_instruction": build_system_instruction(state.memory),
        "tools": TOOLS,
    }
    try:
        ctx     = client.aio.live.connect(model=GEMINI_MODEL, config=config)
        session = await ctx.__aenter__()
        state._gemini_ctx               = ctx
        state.gemini_connected          = True
        state.gemini_reconnect_attempts = 0
        state.memory.record_session_start()
        state.add_log("Gemini session opened")
        log.info("Gemini Live session opened")
        return session
    except Exception as e:
        log.error(f"Gemini connect failed: {e}")
        state.gemini_connected = False
        state.gemini_reconnect_attempts += 1
        return None


async def send_audio_to_gemini(session, pcm_data: bytes):
    """Forward a phone audio chunk straight to Gemini (low-latency path)."""
    if not pcm_data:
        return
    try:
        state.last_activity_time = time.time()
        await session.send_realtime_input(
            audio={"data": pcm_data, "mime_type": "audio/pcm;rate=16000"}
        )
        state.audio_chunks_sent += 1
    except Exception as e:
        log.error(f"send_audio: {e}")


async def send_video_to_gemini(session, jpeg_data: bytes):
    now = time.time()
    if now - state.last_gemini_send_time < GEMINI_FRAME_INTERVAL_S:
        return False
    try:
        await session.send_realtime_input(
            video={"data": jpeg_data, "mime_type": "image/jpeg"}
        )
        state.frames_to_gemini     += 1
        state.last_gemini_send_time = now
        return True
    except Exception as e:
        log.error(f"send_video: {e}")
        return False


# ------------------------------------------------------------
# Tool handling
# ------------------------------------------------------------

async def _respond(session, name: str, fc_id, text: str):
    try:
        await session.send_tool_response(
            function_responses=types.FunctionResponse(
                name=name, response={"result": text}, id=fc_id
            )
        )
    except Exception as e:
        log.error(f"tool response ({name}): {e}")


async def handle_tool_call(session, tool_call):
    if not tool_call or not getattr(tool_call, "function_calls", None):
        return

    for fc in tool_call.function_calls:
        name = getattr(fc, "name", "") or ""
        args = getattr(fc, "args", {}) or {}
        fc_id = getattr(fc, "id", None)

        if name == "remember_face":
            person = _safe_arg(args, "name", 60) or "Unknown"
            state.add_log(f"Memorizing: {person}")
            log.info(f"remember_face: {person}")
            if state.last_frame:
                ok = await asyncio.to_thread(
                    state.face_engine.register_face, state.last_frame, person
                )
                result = "Done." if ok else "No face found in frame."
            else:
                result = "No camera frame available."
            await _respond(session, name, fc_id, result)

        elif name == "save_user_preference":
            key   = _safe_arg(args, "key", 50)
            value = _safe_arg(args, "value", 200)
            log.info(f"save_user_preference: {key} = {value[:60]}")
            k = key.lower()
            if not key or not value:
                result = "Missing key or value."
            elif k in ("ai_name", "your_name", "bot_name"):
                state.memory.set_ai_name(value)
                result = f"I'm {value} now."
            elif k in ("name", "user_name", "my_name"):
                state.memory.set_user_name(value)
                result = f"Got it, {value}."
            elif k in ("mood", "emotion", "feeling"):
                state.memory.update_emotional_state(value)
                result = "Noted."
            elif k in ("style", "communication_style"):
                state.memory.update_conversation_style(value)
                result = "Noted."
            elif k in ("like", "likes"):
                state.memory.add_liked_topic(value)
                result = "Saved."
            elif k in ("dislike", "dislikes"):
                state.memory.add_disliked_topic(value)
                result = "Saved."
            elif k in ("remember", "memory", "important"):
                state.memory.add_memory(value)
                result = "Saved."
            else:
                state.memory.update_preference(key, value)
                result = "Saved."
            await _respond(session, name, fc_id, result)

        elif name == "recall_user_info":
            key = _safe_arg(args, "key", 50) or "all"
            log.info(f"recall_user_info: {key}")
            if key == "all":
                result = state.memory.get_profile_summary()
            else:
                val = state.memory.get_preference(key)
                result = f"{key}: {val}" if val else f"Nothing saved for '{key}'."
            await _respond(session, name, fc_id, result)


# ------------------------------------------------------------
# Receive loop
# ------------------------------------------------------------

_AUTH_PAT  = re.compile(r"\b(401|403|api[_ ]?key|invalid.*key|unauthor)", re.I)
_QUOTA_PAT = re.compile(r"\b(429|quota|rate.?limit)", re.I)


async def receive_from_gemini(session):
    try:
        while True:
            turn = session.receive()
            async for response in turn:
                if response.server_content and response.server_content.model_turn:
                    for part in response.server_content.model_turn.parts:
                        if part.inline_data and isinstance(part.inline_data.data, bytes):
                            state.audio_chunks_received += 1
                            ws = state.phone_ws
                            if ws is not None and not ws.closed:
                                msg = bytes([MSG_TYPE_AUDIO_OUT]) + part.inline_data.data
                                try:
                                    await ws.send_bytes(msg)
                                except Exception as e:
                                    log.error(f"send to phone: {e}")
                        if part.text:
                            state.last_transcript = part.text
                            state.add_log(f"AI: {part.text[:100]}")

                if response.tool_call:
                    await handle_tool_call(session, response.tool_call)

                if response.server_content and response.server_content.interrupted:
                    log.info("Response interrupted")

    except asyncio.CancelledError:
        pass
    except Exception as e:
        err = str(e)
        log.error(f"Gemini receive: {err}")
        state.gemini_connected = False
        state.gemini_session   = None

        if not state.connected:
            log.info("No clients connected — skipping reconnect")
            return

        if _AUTH_PAT.search(err):
            state._auth_failed = True
            state.add_log("Gemini auth error — check API key (auto-reconnect disabled)")
            return  # retrying never helps

        if _QUOTA_PAT.search(err):
            state.add_log("Gemini quota / rate limit — backing off")
            state.gemini_reconnect_attempts = max(state.gemini_reconnect_attempts, 4)

        asyncio.create_task(auto_reconnect_gemini())


# ------------------------------------------------------------
# Face recognition
# ------------------------------------------------------------

async def face_recognition_task():
    """Recognize faces, greet new arrivals, expire old greetings."""
    seen_at: dict[str, float] = {}    # name -> last time greeted
    in_flight = False

    while True:
        try:
            await asyncio.sleep(FACE_RECOGNITION_INTERVAL_S)
            if in_flight or not state.last_frame or not state.gemini_session:
                continue

            frame = state.last_frame
            in_flight = True
            try:
                names = await asyncio.to_thread(state.face_engine.process_frame, frame)
            finally:
                in_flight = False

            now = time.time()
            # Expire stale entries
            for n in list(seen_at):
                if now - seen_at[n] > FACE_FORGET_AFTER_S:
                    del seen_at[n]

            new_arrivals = [n for n in names if n != "Unknown" and n not in seen_at]
            if new_arrivals:
                msg = "System Update: " + ", ".join(sorted(set(new_arrivals))) + " just came into view."
                try:
                    await state.gemini_session.send_realtime_input(text=msg)
                    state.add_log(f"Recognized: {', '.join(sorted(set(new_arrivals)))}")
                except Exception:
                    pass

            for n in names:
                if n != "Unknown":
                    seen_at[n] = now

        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"face task: {e}")
            await asyncio.sleep(5)


# ------------------------------------------------------------
# Reconnect / lifecycle
# ------------------------------------------------------------

async def auto_reconnect_gemini():
    if state._reconnect_in_progress or state._auth_failed:
        return
    async with state._reconnect_lock:
        if state._reconnect_in_progress:
            return
        state._reconnect_in_progress = True
        try:
            while not state.gemini_session:
                if not state.connected or state._auth_failed:
                    return
                delay = min(
                    GEMINI_RECONNECT_BASE_S * (2 ** state.gemini_reconnect_attempts),
                    GEMINI_RECONNECT_MAX_S,
                )
                log.info(f"Reconnecting in {delay:.0f}s...")
                await asyncio.sleep(delay)
                if not state.connected or state._auth_failed:
                    return
                if await ensure_gemini_session():
                    log.info("Gemini reconnected")
                    return
        finally:
            state._reconnect_in_progress = False


async def ensure_gemini_session():
    if state.gemini_session:
        return state.gemini_session
    if state._auth_failed:
        return None

    session = await start_gemini_session()
    if not session:
        return None

    state.gemini_session      = session
    state.gemini_receive_task = asyncio.create_task(receive_from_gemini(session))
    state.idle_check_task     = asyncio.create_task(idle_monitor_task())
    state.face_task           = asyncio.create_task(face_recognition_task())
    if state.flush_task is None or state.flush_task.done():
        state.flush_task = asyncio.create_task(memory_flush_task())
    return session


async def close_gemini_session():
    for attr in ("gemini_receive_task", "idle_check_task", "face_task"):
        task = getattr(state, attr, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            setattr(state, attr, None)

    if state.gemini_session:
        try:
            if state._gemini_ctx:
                await state._gemini_ctx.__aexit__(None, None, None)
                state._gemini_ctx = None
            else:
                await state.gemini_session.close()
        except Exception:
            pass
        state.gemini_session   = None
        state.gemini_connected = False


async def idle_monitor_task():
    while True:
        try:
            await asyncio.sleep(30)
            if not state.gemini_connected:
                continue
            idle = time.time() - state.last_activity_time
            if idle >= IDLE_TIMEOUT_S:
                log.info(f"Idle {int(idle)}s — closing Gemini")
                state.add_log("Idle timeout — session paused")
                await close_gemini_session()
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"idle monitor: {e}")
            await asyncio.sleep(10)


async def memory_flush_task():
    """Coalesce profile writes; survives Gemini restarts."""
    while True:
        try:
            await asyncio.sleep(2.0)
            state.memory.maybe_flush()
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"memory flush: {e}")
            await asyncio.sleep(5)


# ============================================================
# WebSocket handlers
# ============================================================

async def handle_esp32_ws(request: web.Request):
    deny = await _require_auth(request)
    if deny:
        return deny

    ws = web.WebSocketResponse(max_msg_size=WS_MAX_FRAME_BYTES + 16, heartbeat=30)
    await ws.prepare(request)

    state.esp32_ws        = ws
    state.esp32_connected = True
    client_ip = request.remote or "unknown"
    log.info(f"ESP32 connected from {client_ip}")
    state.add_log(f"ESP32 connected from {client_ip}")

    try:
        await ws.send_bytes(bytes([MSG_TYPE_STATUS]) + b"Connected")
    except Exception:
        pass

    await ensure_gemini_session()
    if not state.gemini_session:
        try:
            reason = b"ERROR: Auth failed" if state._auth_failed else b"ERROR: No Gemini API key"
            await ws.send_bytes(bytes([MSG_TYPE_STATUS]) + reason)
        except Exception:
            pass

    if state.phone_ws and not state.phone_ws.closed:
        try:
            await state.phone_ws.send_bytes(bytes([MSG_TYPE_STATUS]) + b"Camera connected")
        except Exception:
            pass

    try:
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.BINARY:
                if msg.type == aiohttp.WSMsgType.ERROR:
                    log.error(f"ESP32 WS error: {ws.exception()}")
                    break
                continue

            data = msg.data
            if len(data) < 2 or data[0] != MSG_TYPE_VIDEO_IN:
                continue
            payload = data[1:]
            if len(payload) > WS_MAX_FRAME_BYTES:
                continue

            # Cheap dedupe — skip identical back-to-back frames (saves Gemini tokens)
            h = hashlib.blake2s(payload, digest_size=8).hexdigest()
            if h == state.last_frame_hash:
                continue
            state.last_frame_hash = h

            state.last_frame      = payload
            state.frames_received += 1
            state.last_frame_time = time.time()
            state.last_activity_time = time.time()

            session = state.gemini_session
            if session is None:
                session = await ensure_gemini_session()
            if session:
                await send_video_to_gemini(session, payload)

    except Exception as e:
        log.error(f"ESP32 handler: {e}")
    finally:
        state.esp32_ws        = None
        state.esp32_connected = False
        log.info("ESP32 disconnected")
        state.add_log("ESP32 disconnected")
        if not state.phone_connected:
            await close_gemini_session()
        state.memory.save()

    return ws


async def handle_phone_ws(request: web.Request):
    deny = await _require_auth(request)
    if deny:
        return deny

    ws = web.WebSocketResponse(max_msg_size=WS_MAX_AUDIO_BYTES + 16, heartbeat=20)
    await ws.prepare(request)

    state.phone_ws        = ws
    state.phone_connected = True
    state.add_log("Phone connected")
    log.info("Phone connected")

    await ensure_gemini_session()

    try:
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.BINARY:
                if msg.type == aiohttp.WSMsgType.ERROR:
                    log.error(f"Phone WS error: {ws.exception()}")
                continue

            data = msg.data
            if len(data) < 2 or data[0] != MSG_TYPE_AUDIO_IN:
                continue
            audio = data[1:]
            if len(audio) > WS_MAX_AUDIO_BYTES:
                continue

            session = state.gemini_session
            if session is None:
                session = await ensure_gemini_session()
            if session:
                await send_audio_to_gemini(session, audio)

    except Exception as e:
        log.error(f"Phone handler: {e}")
    finally:
        state.phone_ws        = None
        state.phone_connected = False
        state.memory.save(force=True)
        state.add_log("Phone disconnected")
        log.info("Phone disconnected")

    return ws


# ============================================================
# HTTP handlers
# ============================================================

async def handle_dashboard(request):
    p = Path(__file__).parent / "dashboard.html"
    return web.FileResponse(p) if p.exists() else web.Response(text="Not found", status=404)


async def handle_phone_page(request):
    p = Path(__file__).parent / "phone.html"
    return web.FileResponse(p) if p.exists() else web.Response(text="Not found", status=404)


async def handle_health(request):
    """Public health check — no auth (used by load balancers)."""
    return web.json_response({"status": "ok", "uptime": int(time.time() - state.start_time)})


async def handle_status_api(request):
    deny = await _require_auth(request)
    if deny:
        return deny
    return web.json_response({
        "esp32_connected":      state.esp32_connected,
        "phone_connected":      state.phone_connected,
        "gemini_connected":     state.gemini_connected,
        "frames_received":      state.frames_received,
        "frames_to_gemini":     state.frames_to_gemini,
        "audio_chunks_sent":    state.audio_chunks_sent,
        "audio_chunks_received": state.audio_chunks_received,
        "last_transcript":      state.last_transcript,
        "uptime_seconds":       int(time.time() - state.start_time),
        "logs":                 state.status_log[-20:],
        "ai_name":              state.memory.profile.get("ai_name", "Buddy"),
        "user_name":            state.memory.profile.get("user_name", ""),
        "total_sessions":       state.memory.profile.get("total_sessions", 0),
    })


async def handle_frame_api(request):
    deny = await _require_auth(request)
    if deny:
        return deny
    if state.last_frame:
        return web.Response(
            body=state.last_frame,
            content_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )
    return web.Response(status=204)


async def handle_profile_get(request):
    deny = await _require_auth(request)
    if deny:
        return deny
    return web.json_response(state.memory.profile)


_NAME_RE = re.compile(r"^[\w \-'.]{1,50}$")


async def handle_profile_post(request):
    deny = await _require_auth(request)
    if deny:
        return deny
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(data, dict):
        return web.json_response({"error": "expected object"}, status=400)

    if "ai_name" in data and isinstance(data["ai_name"], str):
        candidate = data["ai_name"].strip()
        if _NAME_RE.fullmatch(candidate):
            state.memory.set_ai_name(candidate)

    if "user_name" in data and isinstance(data["user_name"], str):
        candidate = data["user_name"].strip()
        if _NAME_RE.fullmatch(candidate):
            state.memory.set_user_name(candidate)

    if (
        "preference_key" in data and "preference_value" in data
        and isinstance(data["preference_key"], str)
        and isinstance(data["preference_value"], str)
    ):
        state.memory.update_preference(
            data["preference_key"], data["preference_value"]
        )

    state.memory.save(force=True)
    return web.json_response({"status": "ok", "profile": state.memory.profile})


# ============================================================
# Lifecycle
# ============================================================

async def main():
    if not GEMINI_API_KEY:
        log.warning("GEMINI_API_KEY is not set!")
    if not AUTH_TOKEN:
        log.warning("AUTH_TOKEN is not set — server accepts anyone on the network!")

    # Best-effort local IP discovery (write to file for the launcher to print)
    import socket
    local_ip = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass
    try:
        (DATA_DIR / "server_ip.txt").write_text(local_ip)
    except Exception:
        pass

    app = web.Application()
    app.router.add_get("/",            handle_dashboard)
    app.router.add_get("/phone",       handle_phone_page)
    app.router.add_get("/ws/phone",    handle_phone_ws)
    app.router.add_get("/ws/esp32",    handle_esp32_ws)
    app.router.add_get("/health",      handle_health)
    app.router.add_get("/api/status",  handle_status_api)
    app.router.add_get("/api/frame",   handle_frame_api)
    app.router.add_get("/api/profile", handle_profile_get)
    app.router.add_post("/api/profile", handle_profile_post)

    runner = web.AppRunner(app)
    await runner.setup()

    await web.TCPSite(runner, "0.0.0.0", HTTP_PORT).start()
    log.info(f"Dashboard : http://localhost:{HTTP_PORT}")
    log.info(f"Phone page: http://{local_ip}:{HTTP_PORT}/phone")
    log.info(f"ESP32 WS  : ws://{local_ip}:{HTTP_PORT}/ws/esp32")

    if SSL_CONTEXT:
        await web.TCPSite(runner, "0.0.0.0", HTTPS_PORT, ssl_context=SSL_CONTEXT).start()
        log.info(f"HTTPS     : https://localhost:{HTTPS_PORT}")

    log.info("Ready — Gemini connects on first client")

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def _signal_stop():
        if not stop.is_set():
            stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _signal_stop)
        except NotImplementedError:
            pass  # Windows

    await stop.wait()
    await shutdown(runner)


async def shutdown(runner=None):
    log.info("Shutting down...")

    if state.phone_ws and not state.phone_ws.closed:
        try:
            await state.phone_ws.send_bytes(bytes([MSG_TYPE_STATUS]) + b"Server shutting down")
        except Exception:
            pass

    await close_gemini_session()
    state.memory.save(force=True)

    for ws in (state.esp32_ws, state.phone_ws):
        if ws and not ws.closed:
            try:
                await ws.close()
            except Exception:
                pass

    if runner is not None:
        try:
            await runner.cleanup()
        except Exception:
            pass

    log.info("Goodbye!")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nServer stopped.")
        sys.exit(0)
