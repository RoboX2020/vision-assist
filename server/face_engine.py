"""
Face engine — recognize and remember faces from JPEG frames.

Lazy-imports `face_recognition` so the rest of the server can boot even
if dlib is missing or the user disables face features.
"""

import json
import logging
import threading
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger("FaceEngine")

MAX_FACES = 150            # cap to keep matching fast and storage bounded
MATCH_THRESHOLD = 0.55     # tighter than default 0.6 — fewer false positives
DOWNSCALE = 0.25           # 4x smaller frame for detection (sufficient at 320x240+)


class FaceEngine:
    def __init__(self, data_dir=None, data_file="faces.json"):
        base = Path(data_dir) if data_dir else Path(__file__).parent
        self.data_file = base / data_file
        self.known_encodings: list[np.ndarray] = []
        self.known_names: list[str] = []
        self._fr = None                       # face_recognition module, lazy
        self._lock = threading.Lock()         # serialize file writes
        self._load_data()

    # ------------------------------------------------------------------
    # Lazy import — avoids paying dlib startup cost unless we actually use it
    # ------------------------------------------------------------------
    def _ensure_loaded(self) -> bool:
        if self._fr is not None:
            return True
        try:
            import face_recognition  # noqa: WPS433 — intentional lazy import
            self._fr = face_recognition
            return True
        except Exception as e:
            log.error(f"face_recognition unavailable; face features disabled ({e})")
            return False

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _load_data(self):
        if not self.data_file.exists():
            log.info("No face database found; starting fresh.")
            return
        try:
            with open(self.data_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for entry in data:
                name = entry.get("name")
                enc = entry.get("encoding")
                if not name or not enc:
                    continue
                self.known_names.append(name)
                self.known_encodings.append(np.asarray(enc, dtype=np.float64))
            log.info(f"Loaded {len(self.known_names)} faces from {self.data_file.name}")
        except Exception as e:
            log.error(f"Failed to load faces: {e}")

    def _save_data(self):
        data = [
            {"name": n, "encoding": e.tolist()}
            for n, e in zip(self.known_names, self.known_encodings)
        ]
        tmp = self.data_file.with_suffix(self.data_file.suffix + ".tmp")
        try:
            with self._lock:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                tmp.replace(self.data_file)         # atomic on POSIX
            log.info(f"Saved {len(self.known_names)} faces")
        except Exception as e:
            log.error(f"Failed to save faces: {e}")
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Recognition
    # ------------------------------------------------------------------
    def process_frame(self, jpeg_bytes: bytes) -> list[str]:
        """Detect & recognize faces. Returns names ('Unknown' if no match)."""
        if not self._ensure_loaded():
            return []
        try:
            arr = np.frombuffer(jpeg_bytes, np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                return []

            small = cv2.resize(img, (0, 0), fx=DOWNSCALE, fy=DOWNSCALE)
            # cvtColor returns a fresh contiguous buffer dlib can use safely.
            rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)

            locations = self._fr.face_locations(rgb, model="hog")
            if not locations:
                return []

            encodings = self._fr.face_encodings(rgb, locations)
            names: list[str] = []
            for enc in encodings:
                name = "Unknown"
                if self.known_encodings:
                    distances = self._fr.face_distance(self.known_encodings, enc)
                    best = int(np.argmin(distances))
                    if distances[best] < MATCH_THRESHOLD:
                        name = self.known_names[best]
                names.append(name)
            return names
        except Exception as e:
            log.error(f"process_frame: {e}")
            return []

    def register_face(self, jpeg_bytes: bytes, name: str) -> bool:
        """Memorize the face in the frame under `name`. Returns True on success."""
        if not self._ensure_loaded():
            return False
        try:
            name = (name or "").strip()
            if not name:
                return False

            arr = np.frombuffer(jpeg_bytes, np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                return False

            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            boxes = self._fr.face_locations(rgb, model="hog")
            if not boxes:
                log.warning("No face found to register.")
                return False

            # Pick the largest face — most likely the subject
            def area(b):
                top, right, bottom, left = b
                return max(0, bottom - top) * max(0, right - left)
            biggest = max(boxes, key=area)

            encoding = self._fr.face_encodings(rgb, [biggest])[0]

            # If this face is already known, just update the name binding.
            if self.known_encodings:
                d = self._fr.face_distance(self.known_encodings, encoding)
                idx = int(np.argmin(d))
                if d[idx] < MATCH_THRESHOLD:
                    self.known_names[idx] = name
                    self._save_data()
                    return True

            self.known_names.append(name)
            self.known_encodings.append(encoding)

            if len(self.known_names) > MAX_FACES:
                self.known_names = self.known_names[-MAX_FACES:]
                self.known_encodings = self.known_encodings[-MAX_FACES:]

            self._save_data()
            return True
        except Exception as e:
            log.error(f"register_face: {e}")
            return False
