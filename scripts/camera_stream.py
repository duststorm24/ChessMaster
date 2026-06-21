#!/usr/bin/env python3
import time

from flask import Flask, Response, render_template_string, jsonify, request
from picamera2 import Picamera2
import cv2
import numpy as np

app = Flask(__name__)

# ---------- CAMERA GLOBALS ----------

picam2 = Picamera2()

# Stream settings
stream_width = 1920
stream_height = 1080
frame_interval = 0.15  # seconds between frames (~6–7 FPS)

# Streaming on/off
streaming_enabled = True

# Auto vs manual exposure/gain
auto_agc = True           # True = automatic exposure & gain (AeEnable)
manual_exposure_us = 10000  # 10 ms
manual_gain = 1.0

latest_stats = {
    "brightness": 0.0,
    "analogue_gain": 0.0,
    "exposure_time": 0.0,    # ms
    "resolution": f"{stream_width}x{stream_height}",
    "fps": round(1.0 / frame_interval, 1),
    "auto_agc": auto_agc,
    "manual_exposure_ms": manual_exposure_us / 1000.0,
    "manual_gain": manual_gain,
}


def apply_camera_controls():
    """Apply current auto/manual exposure & gain settings to the camera."""
    global auto_agc, manual_exposure_us, manual_gain
    controls = {}
    if auto_agc:
        controls["AeEnable"] = True
    else:
        controls["AeEnable"] = False
        controls["ExposureTime"] = int(manual_exposure_us)
        controls["AnalogueGain"] = float(manual_gain)

    try:
        picam2.set_controls(controls)
    except Exception as e:
        print("Control error:", e)


def configure_camera(width: int, height: int):
    """Stop, reconfigure, and restart camera at a new resolution."""
    global stream_width, stream_height, latest_stats

    stream_width = width
    stream_height = height
    latest_stats["resolution"] = f"{width}x{height}"

    picam2.stop()
    config = picam2.create_preview_configuration(
        main={"format": "RGB888", "size": (width, height)}
    )
    picam2.configure(config)
    picam2.start()
    apply_camera_controls()


# Initial camera setup
configure_camera(stream_width, stream_height)


def gen_frames():
    """MJPEG stream generator with FPS throttling and pause support."""
    global latest_stats, frame_interval, streaming_enabled

    while True:
        t0 = time.time()

        if streaming_enabled:
            # Get frame + metadata in one request
            req = picam2.capture_request()
            frame = req.make_array("main")
            meta = req.get_metadata()
            req.release()

            # Compute brightness (0–255 average)
            gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
            brightness = float(gray.mean())

            analogue_gain = float(meta.get("AnalogueGain", 0.0))
            exposure_time = float(meta.get("ExposureTime", 0.0))  # μs

            latest_stats["brightness"] = round(brightness, 1)
            latest_stats["analogue_gain"] = round(analogue_gain, 2)
            latest_stats["exposure_time"] = round(exposure_time / 1000.0, 2)  # ms

            # Encode JPEG
            ret, buffer = cv2.imencode(".jpg", frame)
            if not ret:
                continue

            frame_bytes = buffer.tobytes()

            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + frame_bytes + b"\r\n"
            )
        else:
            # Paused: just sleep a bit and don't capture new frames
            time.sleep(0.1)

        # Throttle FPS
        elapsed = time.time() - t0
        sleep_time = frame_interval - elapsed
        if sleep_time > 0:
            time.sleep(sleep_time)


# ---------- HTML UI ----------

HTML_TEMPLATE = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Pi Camera Control Panel</title>
  <style>
    body {
      margin: 0;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
      background: #050608;
      color: #f5f5f5;
    }
    .page {
      display: flex;
      height: 100vh;
      box-sizing: border-box;
    }
    .video-pane {
      flex: 1;
      display: flex;
      align-items: center;
      justify-content: center;
      background: radial-gradient(circle at top left, #111, #050608 60%);
      padding: 12px;
    }
    .video-frame {
      max-width: 100%;
      max-height: 100%;
      border-radius: 12px;
      box-shadow: 0 0 24px rgba(0,0,0,0.7);
      border: 2px solid rgba(255,255,255,0.1);
      background: #000;
    }
    .control-pane {
      width: 380px;
      padding: 16px 18px;
      border-left: 1px solid rgba(255,255,255,0.08);
      background: radial-gradient(circle at top left, #13151b, #050608 70%);
      box-sizing: border-box;
      overflow-y: auto;
    }
    h1 {
      font-size: 18px;
      letter-spacing: 0.15em;
      text-transform: uppercase;
      margin: 0 0 4px;
    }
    .sub {
      font-size: 11px;
      opacity: 0.7;
      margin-bottom: 10px;
    }
    .status {
      font-size: 12px;
      margin-bottom: 10px;
    }
    .status strong {
      font-weight: 600;
    }
    .section-title {
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.16em;
      margin-top: 12px;
      margin-bottom: 6px;
      opacity: 0.9;
    }
    .card {
      border-radius: 10px;
      padding: 10px 12px;
      background: rgba(0,0,0,0.35);
      box-shadow: 0 0 16px rgba(0,0,0,0.5);
      margin-bottom: 10px;
    }
    label {
      font-size: 12px;
      display: block;
      margin-bottom: 4px;
    }
    select, input[type=number], input[type=range] {
      width: 100%;
      box-sizing: border-box;
      padding: 4px 6px;
      border-radius: 6px;
      border: 1px solid rgba(255,255,255,0.18);
      background: rgba(10,10,12,0.9);
      color: #f5f5f5;
      font-size: 12px;
      margin-bottom: 8px;
    }
    input[type=range] {
      padding: 0;
    }
    .btn {
      border-radius: 999px;
      border: 1px solid rgba(255,255,255,0.25);
      background: linear-gradient(135deg, #2ecc71, #0f9d58);
      color: #020303;
      padding: 6px 14px;
      font-size: 12px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      cursor: pointer;
      width: 100%;
      margin-top: 4px;
    }
    .btn-alt {
      background: linear-gradient(135deg, #ffa726, #fb8c00);
    }
    .btn:disabled {
      opacity: 0.4;
      cursor: default;
    }
    .row {
      display: flex;
      justify-content: space-between;
      gap: 8px;
    }
    .row > div {
      flex: 1;
    }
    .stat-row {
      display: flex;
      justify-content: space-between;
      font-size: 12px;
      margin-bottom: 4px;
    }
    .stat-label {
      opacity: 0.8;
    }
    .stat-value {
      font-weight: 600;
    }
  </style>
</head>
<body>
  <div class="page">
    <div class="video-pane">
      <img class="video-frame" src="/video_feed" alt="Live camera stream">
    </div>
    <div class="control-pane">
      <h1>PI CAMERA</h1>
      <div class="sub">Live stream • Resolution / FPS / exposure / gain</div>
      <div class="status">
        Status: <strong id="status-label">Playing live</strong>
      </div>

      <div class="section-title">Streaming settings</div>
      <div class="card">
        <label for="resolution">Resolution</label>
        <select id="resolution">
          <option value="640x480">640 × 480</option>
          <option value="1280x720">1280 × 720</option>
          <option value="1920x1080" selected>1920 × 1080</option>
        </select>

        <label for="fps">Target FPS (<span id="fps-label">7</span>)</label>
        <input id="fps" type="range" min="2" max="20" step="1" value="7">

        <div class="row">
          <button class="btn-alt btn" id="playpause-btn">Pause stream</button>
          <button class="btn" id="save-btn">Save settings & play</button>
        </div>
      </div>

      <div class="section-title">Exposure & gain</div>
      <div class="card">
        <label>
          <input type="checkbox" id="auto-agc" checked>
          Auto exposure & gain
        </label>

        <label for="exp-ms">Manual exposure (ms)</label>
        <input id="exp-ms" type="number" min="0.1" max="50" step="0.1" value="10.0" disabled>

        <label for="gain">Manual gain</label>
        <input id="gain" type="number" min="1.0" max="16.0" step="0.1" value="1.0" disabled>
      </div>

      <div class="section-title">Live camera stats</div>
      <div class="card">
        <div class="stat-row">
          <div class="stat-label">Resolution</div>
          <div class="stat-value" id="stat-res">–</div>
        </div>
        <div class="stat-row">
          <div class="stat-label">Approx. FPS</div>
          <div class="stat-value" id="stat-fps">–</div>
        </div>
        <div class="stat-row">
          <div class="stat-label">Brightness (0–255)</div>
          <div class="stat-value" id="stat-bright">–</div>
        </div>
        <div class="stat-row">
          <div class="stat-label">Analogue gain</div>
          <div class="stat-value" id="stat-gain">–</div>
        </div>
        <div class="stat-row">
          <div class="stat-label">Exposure time (ms)</div>
          <div class="stat-value" id="stat-exp">–</div>
        </div>
      </div>
    </div>
  </div>

  <script>
    const fpsSlider   = document.getElementById('fps');
    const fpsLabel    = document.getElementById('fps-label');
    const resSelect   = document.getElementById('resolution');
    const playPauseBtn= document.getElementById('playpause-btn');
    const saveBtn     = document.getElementById('save-btn');
    const statusLabel = document.getElementById('status-label');

    const autoAgc     = document.getElementById('auto-agc');
    const expInput    = document.getElementById('exp-ms');
    const gainInput   = document.getElementById('gain');

    const statRes     = document.getElementById('stat-res');
    const statFps     = document.getElementById('stat-fps');
    const statBright  = document.getElementById('stat-bright');
    const statGain    = document.getElementById('stat-gain');
    const statExp     = document.getElementById('stat-exp');

    let isPaused = false;
    let pendingChanges = false;
    let uiInitialized = false;

    function updateStatus() {
      if (isPaused) {
        statusLabel.textContent = pendingChanges ? "Paused (pending changes)" : "Paused";
        playPauseBtn.textContent = "Play stream";
      } else {
        statusLabel.textContent = "Playing live";
        playPauseBtn.textContent = "Pause stream";
      }
    }

    function markDirty() {
      pendingChanges = true;
      // auto-pause once when editing
      if (!isPaused) {
        fetch('/pause', {method: 'POST'});
        isPaused = true;
      }
      updateStatus();
    }

    fpsSlider.addEventListener('input', () => {
      fpsLabel.textContent = fpsSlider.value;
      markDirty();
    });

    resSelect.addEventListener('change', () => {
      markDirty();
    });

    autoAgc.addEventListener('change', () => {
      const auto = autoAgc.checked;
      expInput.disabled = auto;
      gainInput.disabled = auto;
      markDirty();
    });

    expInput.addEventListener('input', markDirty);
    gainInput.addEventListener('input', markDirty);

    playPauseBtn.addEventListener('click', () => {
      if (isPaused) {
        // Resume without changing settings
        fetch('/resume', {method: 'POST'});
        isPaused = false;
        pendingChanges = false;
        updateStatus();
      } else {
        // Manual pause
        fetch('/pause', {method: 'POST'});
        isPaused = true;
        updateStatus();
      }
    });

    saveBtn.addEventListener('click', () => {
      const payload = {
        fps: parseFloat(fpsSlider.value),
        resolution: resSelect.value,
        auto_agc: autoAgc.checked,
        manual_exposure_ms: parseFloat(expInput.value || "10.0"),
        manual_gain: parseFloat(gainInput.value || "1.0")
      };

      fetch('/set_params', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
      })
      .then(() => {
        pendingChanges = false;
        // After saving, resume stream
        if (isPaused) {
          return fetch('/resume', {method: 'POST'});
        }
      })
      .finally(() => {
        isPaused = false;
        updateStatus();
      });
    });

    function refreshStats() {
      fetch('/camera_stats')
        .then(r => r.json())
        .then(data => {
          statRes.textContent    = data.resolution || '–';
          statFps.textContent    = data.fps !== undefined ? data.fps.toFixed(1) : '–';
          statBright.textContent = data.brightness ?? '–';
          statGain.textContent   = data.analogue_gain ?? '–';
          statExp.textContent    = data.exposure_time ?? '–';

          if (!uiInitialized) {
            // Initialize controls ONCE from backend
            if (data.resolution) {
              resSelect.value = data.resolution;
            }
            if (data.fps) {
              const f = Math.max(2, Math.min(20, Math.round(data.fps)));
              fpsSlider.value = f;
              fpsLabel.textContent = f;
            }
            if (data.auto_agc !== undefined) {
              autoAgc.checked = data.auto_agc;
              expInput.disabled = data.auto_agc;
              gainInput.disabled = data.auto_agc;
            }
            if (data.manual_exposure_ms !== undefined) {
              expInput.value = data.manual_exposure_ms.toFixed(2);
            }
            if (data.manual_gain !== undefined) {
              gainInput.value = data.manual_gain.toFixed(2);
            }
            uiInitialized = true;
          }
        })
        .catch(err => console.error('stats error', err));
    }

    // Poll stats every 0.5s
    setInterval(refreshStats, 500);
    refreshStats();
    updateStatus();
  </script>
</body>
</html>
"""


# ---------- FLASK ROUTES ----------

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/video_feed")
def video_feed():
    return Response(gen_frames(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/camera_stats")
def camera_stats():
    return jsonify(latest_stats)


@app.route("/pause", methods=["POST"])
def pause():
    global streaming_enabled
    streaming_enabled = False
    return ("", 200)


@app.route("/resume", methods=["POST"])
def resume():
    global streaming_enabled
    streaming_enabled = True
    return ("", 200)


@app.route("/set_params", methods=["POST"])
def set_params():
    global frame_interval, latest_stats, auto_agc, manual_exposure_us, manual_gain

    data = request.get_json(force=True, silent=True) or {}

    # FPS
    fps = data.get("fps")
    if fps:
        try:
            fps = float(fps)
            fps = max(2.0, min(20.0, fps))
            frame_interval = 1.0 / fps
            latest_stats["fps"] = round(fps, 1)
        except (ValueError, TypeError):
            pass

    # Resolution
    res = data.get("resolution")
    if isinstance(res, str) and "x" in res:
        try:
            w_str, h_str = res.split("x")
            w = int(w_str)
            h = int(h_str)
            if (w, h) in [(640, 480), (1280, 720), (1920, 1080)]:
                configure_camera(w, h)
        except ValueError:
            pass

    # Auto / manual exposure + gain
    agc_flag = data.get("auto_agc")
    if isinstance(agc_flag, bool):
        auto_agc = agc_flag

    exp_ms = data.get("manual_exposure_ms")
    if exp_ms is not None:
        try:
            exp_ms = float(exp_ms)
            exp_ms = max(0.1, min(50.0, exp_ms))
            manual_exposure_us = int(exp_ms * 1000.0)
            latest_stats["manual_exposure_ms"] = exp_ms
        except (ValueError, TypeError):
            pass

    gain_val = data.get("manual_gain")
    if gain_val is not None:
        try:
            gain_val = float(gain_val)
            gain_val = max(1.0, min(16.0, gain_val))
            manual_gain = gain_val
            latest_stats["manual_gain"] = gain_val
        except (ValueError, TypeError):
            pass

    latest_stats["auto_agc"] = auto_agc

    apply_camera_controls()

    return ("", 200)


# ---------- MAIN ----------

if __name__ == "__main__":
    try:
        app.run(host="0.0.0.0", port=5001)
    finally:
        picam2.stop()
