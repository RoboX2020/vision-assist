import time
import json
import os
import logging
import cv2
import numpy as np
from ultralytics import YOLO

log = logging.getLogger("VisionAssist.ObjectEngine")

class ObjectEngine:
    def __init__(self, db_file="memory.json"):
        log.info("Loading YOLOv8n model...")
        self.model = YOLO("yolov8n.pt")  # Auto-downloads to current dir if missing
        self.db_file = db_file
        self.last_seen = {}  # {label: timestamp}
        self.log_cooldown = 10.0  # Seconds before logging the same object again
        
        # Ensure DB file exists
        if not os.path.exists(self.db_file):
            with open(self.db_file, "w") as f:
                json.dump([], f)

    def process_frame(self, frame_bytes):
        """
        Detects objects in the frame and logs them to memory.json.
        Returns a list of detected labels.
        """
        try:
            # Decode JPEG
            nparr = np.frombuffer(frame_bytes, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is None:
                return []

            # Run Interface
            # verbose=False prevents printing to stdout
            results = self.model(img, verbose=False)
            
            detected_labels = []
            current_time = time.time()
            new_entries = []

            for r in results:
                for box in r.boxes:
                    conf = float(box.conf[0])
                    if conf < 0.5: continue  # Filter low confidence
                    
                    cls_id = int(box.cls[0])
                    label = self.model.names[cls_id]
                    
                    if label not in detected_labels:
                        detected_labels.append(label)
                    
                    # Log to memory if cooldown passed
                    if self.should_log(label, current_time):
                        entry = {
                            "timestamp": current_time,
                            "date": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(current_time)),
                            "label": label,
                            "confidence": round(conf, 2)
                        }
                        new_entries.append(entry)
                        self.last_seen[label] = current_time

            if new_entries:
                self.save_entries(new_entries)
                
            return detected_labels

        except Exception as e:
            log.error(f"Object detection error: {e}")
            return []

    def should_log(self, label, now):
        last = self.last_seen.get(label, 0)
        return (now - last) > self.log_cooldown

    def save_entries(self, entries):
        """Appends new entries to the JSON file efficiently."""
        try:
            # Read existing
            if os.path.exists(self.db_file):
                with open(self.db_file, "r") as f:
                    try:
                        data = json.load(f)
                    except json.JSONDecodeError:
                        data = []
            else:
                data = []

            # Append
            data.extend(entries)
            
            # Keep only last 1000 entries to prevent infinite growth
            if len(data) > 1000:
                data = data[-1000:]

            # Write back
            with open(self.db_file, "w") as f:
                json.dump(data, f, indent=2)
                
        except Exception as e:
            log.error(f"Failed to save to memory.json: {e}")
