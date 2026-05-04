"""
Memory Engine — persistent user profile and personalization.

Stores user preferences, conversation style, and important memories in a
JSON file that survives restarts. Writes are atomic (tmp + rename) and
coalesced via a tiny dirty-flag debouncer so rapid updates don't pound disk.
"""

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("VisionAssist.Memory")

_SAVE_INTERVAL_S = 1.5  # coalesce dirty writes for this long
_MAX_TEXT = 500          # cap any single memory/preference value


def _clean(text: str, limit: int = _MAX_TEXT) -> str:
    """Strip control chars and trim to a sane length."""
    if not isinstance(text, str):
        text = str(text)
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    return text.strip()[:limit]


class MemoryEngine:
    """Manages persistent user profile data."""

    DEFAULT_PROFILE = {
        "ai_name": "Buddy",
        "user_name": "",
        "personality_notes": [],
        "preferences": {},
        "emotional_state": "",
        "conversation_style": "",
        "liked_topics": [],
        "disliked_topics": [],
        "important_memories": [],
        "first_interaction": "",
        "total_sessions": 0,
        "last_session": "",
    }

    def __init__(self, profile_path=None):
        self.profile_path = (
            Path(profile_path) if profile_path
            else Path(__file__).parent / "user_profile.json"
        )
        self.profile: dict = {}
        self._lock = threading.Lock()
        self._dirty_at: float = 0.0
        self.load()

    # ------------------------------------------------------------------
    # Load / Save
    # ------------------------------------------------------------------
    def load(self):
        if self.profile_path.exists():
            try:
                with open(self.profile_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                # Merge over defaults so newly added fields don't crash callers.
                self.profile = {**self.DEFAULT_PROFILE, **loaded}
                log.info(f"Loaded user profile from {self.profile_path.name}")
                return
            except Exception as e:
                log.error(f"Profile load failed, using defaults: {e}")

        self.profile = dict(self.DEFAULT_PROFILE)
        self.profile["first_interaction"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        self._write_now()
        log.info("Created new user profile")

    def _mark_dirty(self):
        self._dirty_at = time.time()

    def _write_now(self):
        """Atomic write — never leave a partial file on disk."""
        tmp = self.profile_path.with_suffix(self.profile_path.suffix + ".tmp")
        try:
            with self._lock:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.profile, f, indent=2, ensure_ascii=False)
                tmp.replace(self.profile_path)
        except Exception as e:
            log.error(f"Profile save failed: {e}")
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    def save(self, force: bool = False):
        """Flush dirty state. Coalesces rapid writes unless force=True."""
        if force:
            self._write_now()
            self._dirty_at = 0.0
            return
        if not self._dirty_at:
            return
        # Always write — caller decides cadence; we just guarantee atomicity.
        self._write_now()
        self._dirty_at = 0.0

    def maybe_flush(self):
        """Call from a periodic task to flush coalesced writes."""
        if self._dirty_at and (time.time() - self._dirty_at) >= _SAVE_INTERVAL_S:
            self.save()

    # ------------------------------------------------------------------
    # Session tracking
    # ------------------------------------------------------------------
    def record_session_start(self):
        self.profile["total_sessions"] = self.profile.get("total_sessions", 0) + 1
        self.profile["last_session"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        self._mark_dirty()

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------
    def update_preference(self, key: str, value: str):
        key = _clean(key, 50)
        value = _clean(value, 200)
        if not key:
            return
        self.profile.setdefault("preferences", {})[key] = value
        self._mark_dirty()
        log.info(f"pref: {key} = {value[:60]}")

    def get_preference(self, key: str) -> str:
        return self.profile.get("preferences", {}).get(_clean(key, 50), "")

    def add_memory(self, memory: str):
        memory = _clean(memory)
        if not memory:
            return
        memories = self.profile.setdefault("important_memories", [])
        if memory in memories:
            return
        memories.append(memory)
        if len(memories) > 100:
            self.profile["important_memories"] = memories[-100:]
        self._mark_dirty()
        log.info(f"memory: {memory[:60]}")

    def set_ai_name(self, name: str):
        name = _clean(name, 50)
        if not name:
            return
        self.profile["ai_name"] = name
        self._mark_dirty()
        log.info(f"ai_name: {name}")

    def set_user_name(self, name: str):
        name = _clean(name, 50)
        if not name:
            return
        self.profile["user_name"] = name
        self._mark_dirty()
        log.info(f"user_name: {name}")

    def update_emotional_state(self, state_str: str):
        self.profile["emotional_state"] = _clean(state_str, 100)
        self._mark_dirty()

    def update_conversation_style(self, style: str):
        self.profile["conversation_style"] = _clean(style, 100)
        self._mark_dirty()

    def add_liked_topic(self, topic: str):
        topic = _clean(topic, 80)
        if not topic:
            return
        liked = self.profile.setdefault("liked_topics", [])
        if topic.lower() not in (t.lower() for t in liked):
            liked.append(topic)
            if len(liked) > 50:
                self.profile["liked_topics"] = liked[-50:]
            self._mark_dirty()

    def add_disliked_topic(self, topic: str):
        topic = _clean(topic, 80)
        if not topic:
            return
        disliked = self.profile.setdefault("disliked_topics", [])
        if topic.lower() not in (t.lower() for t in disliked):
            disliked.append(topic)
            if len(disliked) > 50:
                self.profile["disliked_topics"] = disliked[-50:]
            self._mark_dirty()

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    def get_profile_summary(self) -> str:
        """Compact text summary for the system instruction. Token-conscious."""
        p = self.profile
        lines = []

        if p.get("user_name"):
            lines.append(f"- Name: {p['user_name']}")
        if p.get("conversation_style"):
            lines.append(f"- Style: {p['conversation_style']}")
        if p.get("emotional_state"):
            lines.append(f"- Recent mood: {p['emotional_state']}")

        prefs = p.get("preferences") or {}
        if prefs:
            pref_str = ", ".join(f"{k}: {v}" for k, v in list(prefs.items())[:10])
            lines.append(f"- Preferences: {pref_str}")

        liked = p.get("liked_topics") or []
        if liked:
            lines.append(f"- Likes: {', '.join(liked[:8])}")

        disliked = p.get("disliked_topics") or []
        if disliked:
            lines.append(f"- Avoids: {', '.join(disliked[:8])}")

        memories = p.get("important_memories") or []
        if memories:
            lines.append(f"- Notes: {'; '.join(memories[-8:])}")

        sessions = p.get("total_sessions", 0)
        if sessions > 1:
            lines.append(f"- Sessions: {sessions}")

        return "\n".join(lines) if lines else "First time meeting this user."
