# Deploying Vision Assist to Fly.io

Fly.io gives you a persistent cloud VM with automatic HTTPS, WebSocket support,
and a persistent disk for your face database and user profile — accessible from anywhere.

---

## Why Fly.io?

| Requirement | Fly.io |
|---|---|
| WebSockets (ESP32 + phone) | ✅ Full support |
| Persistent storage (faces.json, profile) | ✅ Fly Volumes |
| Auto HTTPS (phone mic needs it) | ✅ Handled at edge |
| Always-on (no cold starts) | ✅ `min_machines_running = 1` |
| dlib / face_recognition build | ✅ Docker-based |
| Free tier | ✅ 3 shared VMs + 3 GB storage |

---

## One-time setup

### 1. Install flyctl

```bash
brew install flyctl        # macOS
# or: curl -L https://fly.io/install.sh | sh
```

### 2. Log in / sign up

```bash
fly auth login
```

### 3. Create the app (first deploy only)

From the repo root:

```bash
fly apps create vision-assist   # choose a unique name
```

If you used a different name, update `fly.toml`:
```toml
app = "your-chosen-name"
```

### 4. Create the persistent volume

```bash
fly volumes create vision_assist_data --size 1 --region ord
```

> Change `ord` to your nearest region (e.g. `sin` Singapore, `lhr` London, `syd` Sydney).
> Also update `primary_region` in `fly.toml` to match.

### 5. Set secrets

```bash
fly secrets set GEMINI_API_KEY="your_gemini_api_key"
fly secrets set AUTH_TOKEN="$(openssl rand -hex 24)"
```

**Save the AUTH_TOKEN value** — you'll need it for the ESP32 config and phone URL.

### 6. Deploy

```bash
fly deploy
```

The first deploy compiles dlib (~5–10 min). Subsequent deploys are fast.

Your app will be live at:
```
https://vision-assist.fly.dev
```

---

## After deploying

### Phone page
Open on your phone (bookmark this):
```
https://vision-assist.fly.dev/phone?token=YOUR_AUTH_TOKEN
```

The token in the URL is forwarded to the WebSocket automatically.

### Dashboard
```
https://vision-assist.fly.dev/?token=YOUR_AUTH_TOKEN
```

### ESP32 firmware
Update `esp32_firmware/config.h`:

```cpp
#define SERVER_HOST   "vision-assist.fly.dev"
#define SERVER_PORT   443
#define SERVER_PATH   "/ws/esp32"
#define SERVER_TOKEN  "YOUR_AUTH_TOKEN"
```

> The ESP32 will need internet access on whatever network it's connected to.
> For WSS (secure WebSocket) on port 443, use `client.connectSSL()` in the firmware.

---

## Ongoing operations

```bash
fly deploy          # push code changes
fly logs            # stream live logs
fly ssh console     # SSH into the VM
fly volumes list    # check your persistent volume
```

---

## Local development (hotspot mode)

Nothing changes for local use:

```bash
cd server
python server.py
```

Phone page:
```
http://192.168.x.x:8080/phone?token=YOUR_LOCAL_TOKEN
```

Set `AUTH_TOKEN` in `server/.env` for local dev:
```
GEMINI_API_KEY=your_key
AUTH_TOKEN=devtoken
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| First `fly deploy` is very slow | dlib compiles from source — ~10 min, normal |
| Phone mic not working | Must use HTTPS — use the `.fly.dev` URL, not HTTP |
| ESP32 can't connect to cloud | It needs internet on its WiFi network |
| Faces/profile lost after redeploy | Create the volume (step 4) — only needs to be done once |
| 401 Unauthorized | Token mismatch — recheck `SERVER_TOKEN` in config.h |
