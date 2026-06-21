#!/usr/bin/env python3
import time
from pathlib import Path
import cv2
import numpy as np
from flask import Flask, Response, render_template_string
from picamera2 import Picamera2

# ---- PATHS TO YOLO FILES ----
MODEL_DIR = Path(__file__).resolve().parents[1] / "models"
WEIGHTS_PATH = str(MODEL_DIR / "yolov3-tiny.weights")
CFG_PATH = str(MODEL_DIR / "yolov3-tiny.cfg")
NAMES_PATH = str(MODEL_DIR / "coco.names")

# ---- LOAD CLASS NAMES ----
with open(NAMES_PATH, "r") as f:
    CLASS_NAMES = [c.strip() for c in f.readlines()]

# ---- LOAD YOLOv3-TINY ----
net = cv2.dnn.readNetFromDarknet(CFG_PATH, WEIGHTS_PATH)
net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)

layer_names = net.getLayerNames()
output_layers = [layer_names[i - 1] for i in net.getUnconnectedOutLayers().flatten()]

# ---- SET UP CAMERA ----
picam2 = Picamera2()
cam_config = picam2.create_video_configuration(
    main={"size": (640, 480), "format": "RGB888"}
)
picam2.configure(cam_config)
picam2.start()

# ---- FLASK APP ----
app = Flask(__name__)

HTML = """
<!doctype html>
<html>
  <head>
    <title>YOLO Object Detection Stream</title>
    <style>
      body {
        background: #111;
        color: #eee;
        font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
        text-align: center;
        margin: 0;
        padding: 20px;
      }
      h1 {
        margin-bottom: 10px;
      }
      .info {
        font-size: 13px;
        opacity: 0.8;
        margin-bottom: 12px;
      }
      img {
        border-radius: 8px;
        box-shadow: 0 0 20px rgba(0,0,0,0.8);
        max-width: 95vw;
        height: auto;
      }
    </style>
  </head>
  <body>
    <h1>YOLOv3-Tiny Live Detection</h1>
    <div class="info">
      Raspberry Pi 5 • Picamera2 • YOLOv3-Tiny<br/>
      If the stream freezes, reload the page.
    </div>
    <img src="/video_feed" />
  </body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML)

def detect_and_draw(frame_bgr):
    """
    Run YOLO on a BGR frame and draw boxes + labels.
    """
    h, w = frame_bgr.shape[:2]

    # Create blob (YOLO expects 416 or 320 etc; we'll use 320 for speed)
    blob = cv2.dnn.blobFromImage(
        frame_bgr, 1/255.0, (320, 320), swapRB=False, crop=False
    )
    net.setInput(blob)
    outputs = net.forward(output_layers)

    boxes = []
    confidences = []
    class_ids = []

    conf_threshold = 0.4
    nms_threshold = 0.3

    for output in outputs:
        for detection in output:
            scores = detection[5:]
            class_id = int(np.argmax(scores))
            confidence = float(scores[class_id])
            if confidence > conf_threshold:
                center_x = int(detection[0] * w)
                center_y = int(detection[1] * h)
                width    = int(detection[2] * w)
                height   = int(detection[3] * h)

                x = int(center_x - width / 2)
                y = int(center_y - height / 2)

                boxes.append([x, y, width, height])
                confidences.append(confidence)
                class_ids.append(class_id)

    idxs = cv2.dnn.NMSBoxes(boxes, confidences, conf_threshold, nms_threshold)

    if len(idxs) > 0:
        for i in idxs.flatten():
            x, y, w_box, h_box = boxes[i]
            label = CLASS_NAMES[class_ids[i]] if class_ids[i] < len(CLASS_NAMES) else "obj"
            conf  = confidences[i]

            # Choose color by class_id
            color = (0, 255, 255)  # yellow-ish
            cv2.rectangle(frame_bgr, (x, y), (x + w_box, y + h_box), color, 2)

            text = f"{label} {conf:.2f}"
            (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
            cv2.rectangle(frame_bgr, (x, y - th - 4), (x + tw, y), color, -1)
            cv2.putText(frame_bgr, text, (x, y - 2),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 1, cv2.LINE_AA)

    return frame_bgr

def generate_frames():
    while True:
        # Capture frame from Picamera2
        frame_rgb = picam2.capture_array()
        frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)

        # Run detection
        frame_bgr = detect_and_draw(frame_bgr)

        # JPEG encode
        ret, jpeg = cv2.imencode(".jpg", frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if not ret:
            continue

        frame_bytes = jpeg.tobytes()

        yield (b"--frame\r\n"
               b"Content-Type: image/jpeg\r\n\r\n" + frame_bytes + b"\r\n")

        # Throttle FPS a bit so the Pi doesn't cook itself
        time.sleep(0.15)  # ~6–7 fps

@app.route("/video_feed")
def video_feed():
    return Response(
        generate_frames(),
        mimetype="multipart/x-mixed-replace; boundary=frame"
    )

if __name__ == "__main__":
    try:
        app.run(host="0.0.0.0", port=5002, threaded=True)
    finally:
        picam2.stop()
