#!/usr/bin/env python3
import time
import threading
from pathlib import Path

from flask import Flask, Response, render_template_string, request, redirect, url_for
import cv2
import numpy as np

from picamera2 import Picamera2
from ultralytics import YOLO

# -------- SETTINGS --------

# Path to your YOLO model
MODEL_PATH = str(Path(__file__).resolve().parent / "yolo11n.pt")

# Default confidence threshold ("tolerance")
CONF_THRESHOLD = 0.35  # lower = more boxes, higher = stricter

FRAME_WIDTH = 1920
FRAME_HEIGHT = 1080
JPEG_QUALITY = 80

# --------------------------

app = Flask(__name__)

# Global state
picam2 = None
model = None
conf_lock = threading.Lock()


def init_camera():
    global picam2
    picam2 = Picamera2()
    config = picam2.create_video_configuration(
        main={"size": (FRAME_WIDTH, FRAME_HEIGHT), "format": "RGB888"}
    )
    picam2.configure(config)
    picam2.start()
    time.sleep(0.5)  # small warmup


def init_model():
    global model
    model = YOLO(MODEL_PATH)


def get_conf_threshold():
    with conf_lock:
        return CONF_THRESHOLD


def set_conf_threshold(value: float):
    global CONF_THRESHOLD
    with conf_lock:
        CONF_THRESHOLD = max(0.05, min(0.9, float(value)))


# ---------- STREAM GENERATOR ----------

def gen_frames():
    """
    Capture frames from Pi camera, run YOLO, overlay boxes, and stream as MJPEG.
    """
    while True:
        if picam2 is None or model is None:
            time.sleep(0.1)
            continue

        # Picamera2 gives RGB frame
        frame_rgb = picam2.capture_array()

        # Convert to BGR for OpenCV / YOLO
        frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)

        conf = get_conf_threshold()

        # Run YOLO, disable verbose spam
        results = model.predict(source=frame_bgr, conf=conf, verbose=False)

        # results[0].plot() returns BGR image with boxes drawn
        annotated_bgr = results[0].plot()

        # Encode as JPEG
        ret, buffer = cv2.imencode(
            ".jpg", annotated_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY]
        )
        if not ret:
            continue

        jpg = buffer.tobytes()
        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n"
        )


# ---------- HTML UI ----------

HTML_TEMPLATE = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>YOLO Camera Stream</title>
  <style>
    body {
      margin: 0;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
      background: #111;
      color: #eee;
    }
    .container {
      max-width: 1200px;
      margin: 16px auto;
      padding: 8px 16px 24px;
      background: #181312;
      border-radius: 12px;
      box-shadow: 0 0 30px rgba(0,0,0,0.7);
      display: grid;
      grid-template-columns: 3fr 2fr;
      gap: 12px;
    }
    h1 {
      grid-column: 1 / -1;
      margin-top: 0;
      font-size: 22px;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      color: #f5d48b;
    }
    .video-wrapper {
      border-radius: 10px;
      overflow: hidden;
      background: #000;
      display: flex;
      justify-content: center;
      align-items: center;
    }
    img.stream {
      width: 100%;
      max-width: 100%;
      display: block;
    }
    .panel {
      padding: 10px 12px;
      border-radius: 10px;
      background: #201411;
      box-shadow: 0 0 20px rgba(0,0,0,0.5);
      font-size: 13px;
    }
    .panel h2 {
      margin-top: 0;
      font-size: 14px;
      letter-spacing: 0.16em;
      text-transform: uppercase;
      color: #f4c46a;
    }
    label {
      display: block;
      margin-top: 8px;
    }
    input[type=number] {
      width: 100%;
      padding: 4px 6px;
      margin-top: 2px;
      border-radius: 6px;
      border: 1px solid #555;
      background: #120c0b;
      color: #eee;
    }
    button {
      margin-top: 10px;
      padding: 6px 10px;
      border-radius: 999px;
      border: 1px solid #f5d48b;
      background: #f5a623;
      color: #1b1209;
      font-size: 12px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.1em;
      cursor: pointer;
    }
    button.secondary {
      background: transparent;
      color: #f5d48b;
      margin-left: 8px;
    }
    .status {
      margin-top: 10px;
      font-size: 12px;
      opacity: 0.85;
    }
    .inline-label {
      font-size: 12px;
      opacity: 0.9;
    }
  </style>
</head>
<body>
  <div class="container">
    <h1>YOLO Live – Dusty&apos;s Camera</h1>
    <div class="video-wrapper">
      <img class="stream" id="stream-img" src="{{ stream_url }}" alt="YOLO Stream">
    </div>
    <div class="panel">
      <h2>Detection Controls</h2>
      <div class="inline-label">Current confidence threshold: <strong>{{ conf }}</strong></div>

      <form method="POST" action="{{ url_for('set_conf') }}">
        <label>Confidence (0.05 – 0.9, lower = more boxes)
          <input type="number" name="conf" step="0.05" min="0.05" max="0.9" value="{{ conf }}">
        </label>
        <button type="submit">Apply</button>
      </form>

      <div class="status">
        <p>Tip: If it&apos;s missing objects, try <strong>lowering</strong> the confidence (e.g. 0.25).</p>
        <p>If it&apos;s drawing boxes on random noise, <strong>raise</strong> it (e.g. 0.5).</p>
      </div>

      <div class="status">
        <p>Red things looking blue? That was a BGR/RGB mismatch – fixed in this version.</p>
      </div>
    </div>
  </div>
</body>
</html>
"""


# ---------- ROUTES ----------

@app.route("/")
def index():
    return render_template_string(
        HTML_TEMPLATE,
        stream_url=url_for("video_feed"),
        conf=get_conf_threshold(),
    )


@app.route("/video_feed")
def video_feed():
    return Response(gen_frames(),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/set_conf", methods=["POST"])
def set_conf():
    new_conf = request.form.get("conf", "").strip()
    try:
        val = float(new_conf)
        set_conf_threshold(val)
    except ValueError:
        pass
    return redirect(url_for("index"))


# ---------- MAIN ----------

if __name__ == "__main__":
    init_camera()
    init_model()
    print(f"[INFO] YOLO stream starting on 0.0.0.0:5002 with default conf={CONF_THRESHOLD}")
    app.run(host="0.0.0.0", port=5002, debug=False, threaded=True)
