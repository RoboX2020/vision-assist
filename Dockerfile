FROM python:3.11-slim

# System deps for dlib (face_recognition) and OpenCV
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    libopenblas-dev \
    liblapack-dev \
    libx11-dev \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (layer-cached until requirements change)
COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy server code
COPY server/ .

# Persistent data lives at /data (mounted as a Fly.io volume)
RUN mkdir -p /data
ENV DATA_DIR=/data

EXPOSE 8080

CMD ["python", "server.py"]
