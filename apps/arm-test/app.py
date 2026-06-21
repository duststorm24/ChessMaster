#!/usr/bin/env python3
import time
import serial
from flask import Flask, request, render_template_string, redirect

# Serial port to Arduino
SERIAL_PORT = "/dev/ttyACM0"
BAUD = 115200

ser = serial.Serial(SERIAL_PORT, BAUD, timeout=1)

app = Flask(__name__)

state = {
    "pos_x": 0,
    "pos_y": 0,
    "pos_z": 0,
    "lim_x": 0,
    "lim_y": 0,
    "lim_z": 0,
    "en": 1,
    # UI-speeds (stepper 1, 2, 3)
    "s1_speed": 400,
    "s2_speed": 400,
    "s3_speed": 400,
}


def send_cmd(cmd, expect_reply=True, wait=0.2):
    """Send a command to the Arduino and optionally grab all lines it prints."""
    ser.write((cmd + "\n").encode("ascii"))
    ser.flush()
    if not expect_reply:
        return ""

    time.sleep(wait)
    lines = []
    while ser.in_waiting:
        line = ser.readline().decode("ascii", errors="ignore").strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def refresh_status():
    """Call POS on Arduino and parse positions + limits + EN into state."""
    out = send_cmd("POS", expect_reply=True, wait=0.2)
    # Example expected:
    # POS X=0 Y=0 Z=0 EN=1
    # LIMITS X=0 Y=0 Z=1
    for line in out.splitlines():
        if line.startswith("POS"):
            # POS X=0 Y=0 Z=0 EN=1
            parts = line.split()
            for p in parts:
                if p.startswith("X="):
                    state["pos_x"] = int(p[2:])
                elif p.startswith("Y="):
                    state["pos_y"] = int(p[2:])
                elif p.startswith("Z="):
                    state["pos_z"] = int(p[2:])
                elif p.startswith("EN="):
                    state["en"] = int(p[3:])
        elif line.startswith("LIMITS"):
            # LIMITS X=0 Y=0 Z=1
            parts = line.split()
            for p in parts:
                if p.startswith("X="):
                    state["lim_x"] = int(p[2:])
                elif p.startswith("Y="):
                    state["lim_y"] = int(p[2:])
                elif p.startswith("Z="):
                    state["lim_z"] = int(p[2:])
    return out


HTML = r"""
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>Robot Arm Control</title>
    <style>
      body {
        font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
        background: #111;
        color: #eee;
        margin: 0;
        padding: 16px;
      }
      .container {
        max-width: 900px;
        margin: 0 auto;
        background: #181818;
        padding: 16px 20px 24px;
        border-radius: 12px;
        box-shadow: 0 0 30px rgba(0,0,0,0.6);
      }
      h1 {
        margin-top: 0;
        letter-spacing: 0.1em;
        text-transform: uppercase;
        font-size: 20px;
      }
      table {
        width: 100%;
        border-collapse: collapse;
        margin-top: 12px;
      }
      th, td {
        padding: 6px 8px;
        text-align: left;
      }
      th {
        border-bottom: 1px solid #333;
        font-size: 12px;
        text-transform: uppercase;
        letter-spacing: 0.08em;
      }
      tr:nth-child(even) {
        background: #202020;
      }
      input[type="number"] {
        width: 80px;
        padding: 2px 4px;
        background: #111;
        border: 1px solid #444;
        border-radius: 4px;
        color: #eee;
      }
      .btn {
        padding: 6px 12px;
        border-radius: 999px;
        border: 1px solid #555;
        background: #333;
        color: #eee;
        font-size: 12px;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        cursor: pointer;
      }
      .btn-primary {
        background: #0f9960;
        border-color: #0f9960;
      }
      .btn-danger {
        background: #b3261e;
        border-color: #b3261e;
      }
      .btn-row {
        margin-top: 10px;
        display: flex;
        gap: 8px;
        flex-wrap: wrap;
      }
      .status {
        margin-top: 14px;
        font-size: 13px;
      }
      .badge {
        display: inline-block;
        padding: 2px 8px;
        border-radius: 999px;
        font-size: 11px;
        margin-right: 6px;
      }
      .badge-ok {
        background: #0f9960;
      }
      .badge-warn {
        background: #b3261e;
      }
      .limits {
        margin-top: 8px;
        font-size: 12px;
      }
      .limits span {
        margin-right: 10px;
      }
      .lim-pressed {
        color: #ff8080;
        font-weight: 600;
      }
      .lim-released {
        color: #8fe18f;
      }
      .pos-line {
        font-size: 12px;
        margin-top: 4px;
      }
    </style>
  </head>
  <body>
    <div class="container">
      <h1>Robot Arm Control</h1>

      <form method="post">
        <table>
          <tr>
            <th>Stepper</th>
            <th>Function</th>
            <th>Steps (±)</th>
            <th>Speed (steps/sec)</th>
            <th>Home</th>
          </tr>
          <tr>
            <td>1</td>
            <td>Base (X)</td>
            <td><input type="number" name="s1_steps" value="0" /></td>
            <td><input type="number" name="s1_speed" value="{{ s1_speed }}" /></td>
            <td>
              <button class="btn" name="action" value="home1">Home S1</button>
            </td>
          </tr>
          <tr>
            <td>2</td>
            <td>Arm 1</td>
            <td><input type="number" name="s2_steps" value="0" /></td>
            <td><input type="number" name="s2_speed" value="{{ s2_speed }}" /></td>
            <td>
              <button class="btn" name="action" value="home2">Home S2</button>
            </td>
          </tr>
          <tr>
            <td>3</td>
            <td>Arm 2</td>
            <td><input type="number" name="s3_steps" value="0" /></td>
            <td><input type="number" name="s3_speed" value="{{ s3_speed }}" /></td>
            <td>
              <button class="btn" name="action" value="home3">Home S3</button>
            </td>
          </tr>
        </table>

        <div class="btn-row">
          <button class="btn btn-primary" name="action" value="move">Move</button>
          <button class="btn" name="action" value="refresh">Refresh status</button>
          <button class="btn" name="action" value="energize">Energize</button>
          <button class="btn" name="action" value="deenergize">De-energize</button>
          <button class="btn btn-danger" name="action" value="homeall">Home ALL</button>
        </div>
      </form>

      <div class="status">
        <span class="badge {% if en %}badge-ok{% else %}badge-warn{% endif %}">
          Drivers: {{ "ENABLED" if en else "DISABLED" }}
        </span>
        <div class="pos-line">
          Position (steps from home):
          S1/Base = {{ pos_x }},
          S2/Arm1 = {{ pos_z }},
          S3/Arm2 = {{ pos_y }}
        </div>
        <div class="limits">
          Limits:
          <span>
            X/Base:
            <span class="{{ 'lim-pressed' if lim_x else 'lim-released' }}">
              {{ 'PRESSED' if lim_x else 'RELEASED' }}
            </span>
          </span>
          <span>
            Arm 1:
            <span class="{{ 'lim-pressed' if lim_z else 'lim-released' }}">
              {{ 'PRESSED' if lim_z else 'RELEASED' }}
            </span>
          </span>
          <span>
            Arm 2:
            <span class="{{ 'lim-pressed' if lim_y else 'lim-released' }}">
              {{ 'PRESSED' if lim_y else 'RELEASED' }}
            </span>
          </span>
        </div>
      </div>
    </div>
  </body>
</html>
"""


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        action = request.form.get("action", "")

        # Read speeds from form (keep previous if empty/invalid)
        def get_speed(name, fallback):
            val = request.form.get(name, "")
            try:
                v = int(val)
                if v > 0:
                    return v
            except ValueError:
                pass
            return fallback

        state["s1_speed"] = get_speed("s1_speed", state["s1_speed"])
        state["s2_speed"] = get_speed("s2_speed", state["s2_speed"])
        state["s3_speed"] = get_speed("s3_speed", state["s3_speed"])

        # Map UI speeds (S1,S2,S3) -> firmware axes (X, Y, Z)
        vx = state["s1_speed"]   # X = Stepper 1 (base)
        vz = state["s2_speed"]   # Z = Stepper 2 (arm1)
        vy = state["s3_speed"]   # Y = Stepper 3 (arm2)

        if action == "move":
            # Steps from UI
            def get_steps(name):
                val = request.form.get(name, "0")
                try:
                    return int(val)
                except ValueError:
                    return 0

            s1 = get_steps("s1_steps")  # base
            s2 = get_steps("s2_steps")  # arm1
            s3 = get_steps("s3_steps")  # arm2

            # 1) set per-axis speeds
            send_cmd(f"SPEED {vx} {vy} {vz}", expect_reply=True)

            # 2) map UI steps to firmware axes with sign convention:
            #    +N in UI = AWAY from limit for ALL three steppers
            #
            # From your observations:
            #  - base: "-" was toward limit  -> "+" is away  (firmware + = away)
            #  - arm1: "-" was toward limit -> "+" is away  (firmware + = away)
            #  - arm2: "+" was toward limit -> "-" is away  (firmware - = away)
            #
            # And wiring:
            #  X axis = base   (Stepper 1)
            #  Y axis = arm 2  (Stepper 3)
            #  Z axis = arm 1  (Stepper 2)
            dx = s1                     # base: +UI -> +X (away)
            dz = s2                     # arm1: +UI -> +Z (away)
            dy = -s3                    # arm2: +UI -> -Y (away)

            send_cmd(f"MOVE {dx} {dy} {dz}", expect_reply=True)
            refresh_status()

        elif action == "home1":
            # Home base (X)
            send_cmd(f"SPEED {vx} {vy} {vz}", expect_reply=False)
            send_cmd("HOMEX", expect_reply=True)
            refresh_status()

        elif action == "home2":
            # Home arm1 (Z)
            send_cmd(f"SPEED {vx} {vy} {vz}", expect_reply=False)
            send_cmd("HOMEZ", expect_reply=True)
            refresh_status()

        elif action == "home3":
            # Home arm2 (Y)
            send_cmd(f"SPEED {vx} {vy} {vz}", expect_reply=False)
            send_cmd("HOMEY", expect_reply=True)
            refresh_status()

        elif action == "homeall":
            send_cmd(f"SPEED {vx} {vy} {vz}", expect_reply=False)
            send_cmd("HOMEALL", expect_reply=True)
            refresh_status()

        elif action == "energize":
            send_cmd("ENERGIZE", expect_reply=True)
            refresh_status()

        elif action == "deenergize":
            send_cmd("DEENERGIZE", expect_reply=True)
            refresh_status()

        elif action == "refresh":
            refresh_status()

        # Avoid form re-submit on reload
        return redirect("/")

    # GET: ensure we have fresh status at least once
    if state["pos_x"] == 0 and state["pos_y"] == 0 and state["pos_z"] == 0:
        refresh_status()

    return render_template_string(
        HTML,
        pos_x=state["pos_x"],
        pos_y=state["pos_y"],
        pos_z=state["pos_z"],
        lim_x=state["lim_x"],
        lim_y=state["lim_y"],
        lim_z=state["lim_z"],
        en=state["en"],
        s1_speed=state["s1_speed"],
        s2_speed=state["s2_speed"],
        s3_speed=state["s3_speed"],
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5002)
