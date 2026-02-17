import face_recognition
import cv2
import numpy as np
import json
import os
from pathlib import Path
import logging

log = logging.getLogger("FaceEngine")

class FaceEngine:
    def __init__(self, data_file="faces.json"):
        self.data_file = Path(__file__).parent / data_file
        self.known_encodings = []
        self.known_names = []
        self.load_data()

    def load_data(self):
        """Load face database from JSON."""
        if self.data_file.exists():
            try:
                with open(self.data_file, "r") as f:
                    data = json.load(f)
                    count = 0
                    for entry in data:
                        self.known_names.append(entry["name"])
                        # Encodings are stored as lists, convert back to numpy arrays
                        self.known_encodings.append(np.array(entry["encoding"]))
                        count += 1
                log.info(f"Loaded {count} faces from {self.data_file.name}")
            except Exception as e:
                log.error(f"Failed to load faces: {e}")
        else:
            log.info("No face database found. Starting fresh.")

    def save_data(self):
        """Save face database to JSON."""
        data = []
        for name, enc in zip(self.known_names, self.known_encodings):
            data.append({"name": name, "encoding": enc.tolist()})
        try:
            with open(self.data_file, "w") as f:
                json.dump(data, f)
            log.info(f"Saved {len(self.known_names)} faces to {self.data_file.name}")
        except Exception as e:
            log.error(f"Failed to save faces: {e}")

    def process_frame(self, jpeg_bytes):
        """Detect and recognize faces in a JPEG frame."""
        try:
            # Decode JPEG
            nparr = np.frombuffer(jpeg_bytes, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is None:
                return []

            # Resize for speed (1/4 size) to enable real-time processing
            # OpenCV resizing is very fast
            small_img = cv2.resize(img, (0, 0), fx=0.25, fy=0.25)

            # Convert BGR (OpenCV) to RGB (face_recognition)
            # numpy slicing is efficient
            rgb_small_img = small_img[:, :, ::-1]

            # Detect faces
            # model="hog" is faster than "cnn" (default is hog)
            face_locations = face_recognition.face_locations(rgb_small_img)
            
            if not face_locations:
                return []

            # Compute encodings
            face_encodings = face_recognition.face_encodings(rgb_small_img, face_locations)

            matched_names = []
            for face_encoding in face_encodings:
                name = "Unknown"
                if self.known_encodings:
                    # Compare against known faces
                    matches = face_recognition.compare_faces(self.known_encodings, face_encoding, tolerance=0.6)
                    face_distances = face_recognition.face_distance(self.known_encodings, face_encoding)
                    
                    if True in matches:
                        # Find the best match (smallest distance)
                        best_match_index = np.argmin(face_distances)
                        if matches[best_match_index]:
                            name = self.known_names[best_match_index]
                
                matched_names.append(name)

            return matched_names

        except Exception as e:
            log.error(f"Error processing frame for faces: {e}")
            return []

    def register_face(self, jpeg_bytes, name):
        """Register a new face from a frame. Returns success boolean."""
        try:
            nparr = np.frombuffer(jpeg_bytes, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is None: return False

            rgb_img = img[:, :, ::-1]
            boxes = face_recognition.face_locations(rgb_img)
            
            if not boxes:
                log.warning("No face found to register.")
                return False
            
            # Use the first face found
            encoding = face_recognition.face_encodings(rgb_img, boxes)[0]
            
            # Check if name is new or update existing?
            # For simplicity, we append. If duplicate name, it just adds another sample (actually good for accuracy).
            self.known_names.append(name)
            self.known_encodings.append(encoding)
            self.save_data()
            return True

        except Exception as e:
            log.error(f"Error registering face: {e}")
            return False
