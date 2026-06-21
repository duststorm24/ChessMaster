import re
import time
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import serial
from flask import Flask, render_template
from flask_socketio import SocketIO

# ---------------- Config ----------------
SERIAL_PORT = "/dev/ttyACM0"
BAUD = 115200

# Soft limits (home=0, away is negative)
SOFT_LIMITS = {
    "X": -10500,
    "Y": -49500,
    "Z": -39000,
}

# Broadcast rate to browser (Hz)
STATE_HZ = 30.0

# If we haven't seen a POS line in this many seconds, poke the Uno for POS
POS_STALE_SEC = 0.6

# Safety net: ask for POS at most this often when stale
POS_POKE_PERIOD_SEC = 0.25

# ---------------- App ----------------
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# ---------------- State ----------------
@dataclass
class AxisState:
    pos: Optional[int] = None   # None => unknown => UI shows ?
    homed: bool = False

state_lock = threading.Lock()
axes: Dict[str, AxisState] = {k: AxisState() for k in ["X", "Y", "Z"]}

motors_enabled: Optional[bool] = None
estop_latched: Optional[bool] = None

last_lines: List[str] = []

ser: Optional[serial.Serial] = None
ser_lock = threading.Lock()

_last_state_emit = 0.0
_last_pos_rx_time = 0.0
_last_pos_poke_time = 0.0

# ---------------- Regex ----------------
POS_RE = re.compile(r"POS\s+X=(-?\d+)\s+Y=(-?\d+)\s+Z=(-?\d+)\s+EN=(\d+)", re.IGNORECASE)
HOME_HIT_RE = re.compile(r"HOME_HIT\s+([XYZ])", re.IGNORECASE)
BOOT_HOME_RE = re.compile(r"BOOT_HOME\s+([XYZ])", re.IGNORECASE)
DONE_RE = re.compile(r"DONE:\s*([XYZ])\s+HOMED", re.IGNORECASE)
ESTOP_RE = re.compile(r"ESTOP:", re.IGNORECASE)


def push_log(line: str) -> None:
    global last_lines
    line = line.rstrip("\n")
    with state_lock:
        last_lines.append(line)
        last_lines = last_lines[-400:]
    socketio.emit("log", {"line": line})


def clamp_target(axis: str, target: int) -> int:
    """Clamp absolute target position into [soft_limit, 0]."""
    lo = SOFT_LIMITS[axis]
    hi = 0
    return max(lo, min(hi, target))


def apply_delta_with_limits(dx: int, dy: int, dz: int) -> Tuple[int, int, int, List[str]]:
    """
    Given requested deltas, clamp them so resulting absolute positions remain within soft limits.
    Returns (dx2, dy2, dz2, notes)
    """
    notes = []
    with state_lock:
        cur = {a: axes[a].pos for a in ["X", "Y", "Z"]}
        homed = {a: axes[a].homed for a in ["X", "Y", "Z"]}

    # If an axis delta is nonzero, it must be homed and have a known position
    for a, d in [("X", dx), ("Y", dy), ("Z", dz)]:
        if d != 0 and (not homed[a] or cur[a] is None):
            notes.append(f"BLOCKED: {a} not homed/known; delta ignored")
            if a == "X": dx = 0
            if a == "Y": dy = 0
            if a == "Z": dz = 0

    # Clamp each axis to its soft limits
    def clamp_delta(a: str, d: int) -> int:
        if d == 0:
            return 0
        c = cur[a]
        if c is None:
            return 0
        target = c + d
        target_c = clamp_target(a, target)
        if target_c != target:
            notes.append(f"SOFT_LIMIT: {a} clamped {target} -> {target_c}")
        return target_c - c

    dx2 = clamp_delta("X", dx)
    dy2 = clamp_delta("Y", dy)
    dz2 = clamp_delta("Z", dz)

    return dx2, dy2, dz2, notes


def optimistic_apply(dx: int, dy: int, dz: int) -> None:
    """Update internal positions immediately so UI doesn't repeat moves while waiting for POS lines."""
    with state_lock:
        for a, d in [("X", dx), ("Y", dy), ("Z", dz)]:
            if d != 0 and axes[a].pos is not None:
                axes[a].pos = clamp_target(a, axes[a].pos + d)


def broadcast_state(force: bool = False) -> None:
    global _last_state_emit
    now = time.time()
    if not force and (now - _last_state_emit) < (1.0 / STATE_HZ):
        return
    _last_state_emit = now

    with state_lock:
        payload = {
            "axes": {k: {"pos": v.pos, "homed": v.homed} for k, v in axes.items()},
            "motors_enabled": motors_enabled,
            "estop_latched": estop_latched,
            "soft_limits": SOFT_LIMITS,
            "server_time": now,
        }
    socketio.emit("state", payload)


def write_cmd(cmd: str) -> None:
    global ser
    if not cmd:
        return
    if ser is None:
        push_log("[ERR] Serial not open")
        return

    data = (cmd.strip() + "\n").encode("ascii", errors="ignore")
    with ser_lock:
        try:
            ser.write(data)
        except Exception as e:
            push_log(f"[ERR] Serial write failed: {e}")


def open_serial() -> None:
    global ser
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD, timeout=0.02)
        time.sleep(2.0)  # Uno resets on open
        push_log(f"[INFO] Connected to {SERIAL_PORT} @ {BAUD}")
        # Turn on streaming (if firmware supports it) and request immediate POS
        write_cmd("WATCH ON")
        write_cmd("POS")
    except Exception as e:
        ser = None
        push_log(f"[ERR] Could not open serial {SERIAL_PORT}: {e}")


def serial_reader() -> None:
    global motors_enabled, estop_latched, _last_pos_rx_time

    if ser is None:
        push_log("[ERR] serial_reader started with no serial")
        return

    push_log("[INFO] Serial reader started")

    while True:
        try:
            raw = ser.readline()
            if not raw:
                # no data
                time.sleep(0.001)
                continue
            line = raw.decode("utf-8", errors="ignore").strip()
            if not line:
                continue

            push_log(line)

            if ESTOP_RE.search(line):
                with state_lock:
                    estop_latched = True
                broadcast_state(force=True)
                continue

            if "OK ESTOP=0" in line.upper():
                with state_lock:
                    estop_latched = False
                broadcast_state(force=True)
                continue

            b = BOOT_HOME_RE.search(line)
            if b:
                ax = b.group(1).upper()
                with state_lock:
                    axes[ax].homed = True
                    axes[ax].pos = 0
                broadcast_state(force=True)
                continue

            h = HOME_HIT_RE.search(line)
            if h:
                ax = h.group(1).upper()
                with state_lock:
                    axes[ax].homed = True
                    axes[ax].pos = 0
                broadcast_state(force=True)
                continue

            d = DONE_RE.search(line)
            if d:
                ax = d.group(1).upper()
                with state_lock:
                    axes[ax].homed = True
                    if axes[ax].pos is None:
                        axes[ax].pos = 0
                broadcast_state(force=True)
                continue

            m = POS_RE.search(line)
            if m:
                x = int(m.group(1))
                y = int(m.group(2))
                z = int(m.group(3))
                en = int(m.group(4))

                with state_lock:
                    axes["X"].pos = clamp_target("X", x)
                    axes["Y"].pos = clamp_target("Y", y)
                    axes["Z"].pos = clamp_target("Z", z)
                    motors_enabled = bool(en)
                _last_pos_rx_time = time.time()
                broadcast_state()
                continue

        except Exception as e:
            push_log(f"[ERR] Serial read loop error: {e}")
            time.sleep(0.25)


def pos_poker() -> None:
    """
    Safety net: if POS streaming stalls, poke POS occasionally so UI doesn't lag for 30s.
    """
    global _last_pos_poke_time
    while True:
        try:
            now = time.time()
            if ser is not None:
                stale = (now - _last_pos_rx_time) > POS_STALE_SEC
                can_poke = (now - _last_pos_poke_time) > POS_POKE_PERIOD_SEC
                if stale and can_poke:
                    write_cmd("POS")
                    _last_pos_poke_time = now
            time.sleep(0.05)
        except Exception:
            time.sleep(0.25)


# ---------------- Routes ----------------
@app.route("/")
def index():
    return render_template("index.html")


# ---------------- Socket events ----------------
@socketio.on("connect")
def on_connect():
    # Send some log + current state
    with state_lock:
        lines = list(last_lines)
    for ln in lines[-120:]:
        socketio.emit("log", {"line": ln})

    # Make sure streaming is on and push an immediate POS
    write_cmd("WATCH ON")
    write_cmd("POS")
    broadcast_state(force=True)


@socketio.on("pos")
def on_pos():
    write_cmd("POS")


@socketio.on("watch")
def on_watch(data):
    on = bool(data.get("on", True))
    write_cmd("WATCH ON" if on else "WATCH OFF")


@socketio.on("energize")
def on_energize():
    write_cmd("ENERGIZE")


@socketio.on("deenergize")
def on_deenergize():
    write_cmd("DEENERGIZE")


@socketio.on("estop")
def on_estop():
    write_cmd("ESTOP")


@socketio.on("clear_estop")
def on_clear_estop():
    write_cmd("CLEAR")


@socketio.on("set_speed")
def on_set_speed(data):
    vx = int(data.get("vx", 800))
    vy = int(data.get("vy", 3000))
    vz = int(data.get("vz", 3000))
    write_cmd(f"SPEED {vx} {vy} {vz}")


@socketio.on("limits")
def on_limits():
    write_cmd("LIMITS")


@socketio.on("jog")
def on_jog(data):
    axis = str(data.get("axis", "")).upper()
    steps = int(data.get("steps", 0))
    if axis not in ("X", "Y", "Z"):
        return

    dx = dy = dz = 0
    if axis == "X": dx = steps
    if axis == "Y": dy = steps
    if axis == "Z": dz = steps

    dx2, dy2, dz2, notes = apply_delta_with_limits(dx, dy, dz)
    for n in notes:
        push_log(n)

    if dx2 == 0 and dy2 == 0 and dz2 == 0:
        return

    write_cmd(f"MOVE {dx2} {dy2} {dz2}")
    optimistic_apply(dx2, dy2, dz2)
    broadcast_state(force=True)


@socketio.on("move_delta")
def on_move_delta(data):
    dx = int(data.get("dx", 0))
    dy = int(data.get("dy", 0))
    dz = int(data.get("dz", 0))

    dx2, dy2, dz2, notes = apply_delta_with_limits(dx, dy, dz)
    for n in notes:
        push_log(n)

    if dx2 == 0 and dy2 == 0 and dz2 == 0:
        return

    write_cmd(f"MOVE {dx2} {dy2} {dz2}")
    optimistic_apply(dx2, dy2, dz2)
    broadcast_state(force=True)


@socketio.on("home_axis")
def on_home_axis(data):
    axis = str(data.get("axis", "")).upper()
    if axis not in ("X", "Y", "Z"):
        return
    write_cmd(f"HOME{axis}")
    # While homing, keep pos unknown until we get HOME_HIT/BOOT_HOME
    with state_lock:
        axes[axis].homed = False
        axes[axis].pos = None
    broadcast_state(force=True)


@socketio.on("home_all")
def on_home_all():
    # Try simultaneous firmware command first; fall back to sequential if unsupported
    write_cmd("HOMEALLSIM")
    # If firmware doesn't know HOMEALLSIM it will print unknown; UI still works.
    # Also send HOMEALL after a short delay so something happens even on old firmware.
    def fallback():
        time.sleep(0.15)
        write_cmd("HOMEALL")
    threading.Thread(target=fallback, daemon=True).start()

    with state_lock:
        for a in ["X", "Y", "Z"]:
            axes[a].homed = False
            axes[a].pos = None
    broadcast_state(force=True)


# ---------------- Main ----------------
if __name__ == "__main__":
    open_serial()
    if ser is not None:
        threading.Thread(target=serial_reader, daemon=True).start()
        threading.Thread(target=pos_poker, daemon=True).start()

    socketio.run(app, host="0.0.0.0", port=5000)
