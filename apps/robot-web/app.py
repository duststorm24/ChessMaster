from flask import Flask, render_template, request, jsonify, redirect
import json
import math
import os
import serial
import time
import threading
from serial import SerialException

app = Flask(__name__)
APP_DIR = os.path.dirname(os.path.abspath(__file__))

SERIAL_PORT = "/dev/ttyACM0"
BAUD_RATE = 115200
Z_MAX_DISTANCE_FROM_HOME = 10000
X_MAX_DISTANCE_FROM_HOME = 9000
Y_MAX_DISTANCE_FROM_HOME = 9200
AXIS_MAX_DISTANCES = {
    "Z": Z_MAX_DISTANCE_FROM_HOME,
    "X": X_MAX_DISTANCE_FROM_HOME,
    "Y": Y_MAX_DISTANCE_FROM_HOME,
}
DEMO_AXES = ("Z", "X", "Y")
DEMO_POLL_INTERVAL = 0.2
DEMO_TARGET_TOLERANCE = 10
SEQUENCE_POLL_INTERVAL = 0.2
SEQUENCE_TARGET_TOLERANCE = 10
SEQUENCE_STEP_TIMEOUT = 180.0
POSITIONS_FILE = os.path.join(APP_DIR, "saved_positions.json")
CARTESIAN_LINES_FILE = os.path.join(APP_DIR, "saved_cartesian_lines.json")
MAX_SAVED_CARTESIAN_LINES = 20
MIN_AXIS_SPEED = 25
MAX_AXIS_SPEED = 3000
ROTATING_BASE_HOME_SPEED = 1500
CARTESIAN_POLL_INTERVAL = 0.08
CARTESIAN_TARGET_TOLERANCE = 10
CARTESIAN_STEP_TIMEOUT = 90.0
MIN_CARTESIAN_SEGMENTS = 2
MAX_CARTESIAN_SEGMENTS = 120
POINT_MOVE_POLL_INTERVAL = 0.08
POINT_MOVE_STEP_TIMEOUT = 90.0
BASE_HEIGHT_CM = 16.03
ARM1_LENGTH_CM = 24.08
ARM2_LENGTH_CM = 25.34
BASE_HOME_DEG = 0.0
ARM1_HOME_DEG = 4.76
ARM2_FOLD_BACK_OFFSET_DEG = 14.76
BASE_TRAVEL_DEG = 270.0
ARM1_TRAVEL_DEG = 186.24
ARM2_TRAVEL_DEG = 188.0
BASE_STEPS_PER_DEG = Z_MAX_DISTANCE_FROM_HOME / BASE_TRAVEL_DEG
ARM1_STEPS_PER_DEG = X_MAX_DISTANCE_FROM_HOME / ARM1_TRAVEL_DEG
ARM2_STEPS_PER_DEG = Y_MAX_DISTANCE_FROM_HOME / ARM2_TRAVEL_DEG
ARM2_HOME_RELATIVE_DEG = 180.0 - ARM2_FOLD_BACK_OFFSET_DEG
ARM2_MIN_DEG = ARM2_HOME_RELATIVE_DEG - ARM2_TRAVEL_DEG
ARM2_MAX_DEG = ARM2_HOME_RELATIVE_DEG
ARM_POSE_POSITION_TOLERANCE_CM = 0.05
BASE_BRANCH_WEIGHT = 6.0
ARM_PLANE_OFFSET_DEG = 180.0

ser = None
serial_lock = threading.Lock()
SERIAL_RETRY_DELAY = 0.2

positions_lock = threading.Lock()
cartesian_lines_lock = threading.Lock()
demo_lock = threading.Lock()
demo_thread = None
demo_stop_event = threading.Event()
demo_state = {
    "active": False,
    "message": "Inactive",
    "axes": {},
    "ranges": {},
}
sequence_lock = threading.Lock()
sequence_thread = None
sequence_stop_event = threading.Event()
sequence_state = {
    "active": False,
    "message": "Inactive",
    "steps": [],
    "current_index": None,
    "current_step": None,
    "speeds": {},
}
cartesian_lock = threading.Lock()
cartesian_thread = None
cartesian_stop_event = threading.Event()
cartesian_state = {
    "active": False,
    "message": "Inactive",
    "line": None,
    "current_segment": None,
    "segment_count": 0,
    "max_speeds": {},
}
point_move_lock = threading.Lock()
point_move_thread = None
point_move_stop_event = threading.Event()
point_move_state = {
    "active": False,
    "message": "Inactive",
    "targets": None,
    "current_segment": None,
    "segment_count": 0,
    "max_speeds": {},
}
_STATE_UNSET = object()


def sanitize_axis_speed(value):
    try:
        speed = abs(int(value))
    except (TypeError, ValueError):
        return None

    if speed == 0:
        return None

    return max(MIN_AXIS_SPEED, min(MAX_AXIS_SPEED, speed))


def sanitize_step_count(value, maximum):
    try:
        steps = abs(int(value))
    except (TypeError, ValueError):
        return None

    if steps == 0:
        return None

    steps = max(50, steps)
    return min(maximum, steps)


def parse_distance_response(axis, response):
    prefix = f"DIST_{axis}="
    if not response.startswith(prefix):
        return None

    value = response.split("=", 1)[1]
    if value == "UNKNOWN":
        return "UNKNOWN"

    try:
        return int(value)
    except ValueError:
        return None


def read_axis_distance(axis):
    return parse_distance_response(axis, send_serial_command(f"DIST {axis}"))


def get_direction_for_distance_delta(axis, delta):
    if axis == "X":
        return 1 if delta > 0 else -1

    if axis in ("Y", "Z"):
        return -1 if delta > 0 else 1

    return 1 if delta > 0 else -1


def get_demo_snapshot():
    with demo_lock:
        return {
            "active": demo_state["active"],
            "message": demo_state["message"],
            "axes": {axis: dict(config) for axis, config in demo_state["axes"].items()},
            "ranges": {axis: dict(bounds) for axis, bounds in demo_state["ranges"].items()},
        }


def update_demo_state(*, active=None, message=None, axes=None, ranges=None):
    with demo_lock:
        if active is not None:
            demo_state["active"] = active
        if message is not None:
            demo_state["message"] = message
        if axes is not None:
            demo_state["axes"] = axes
        if ranges is not None:
            demo_state["ranges"] = ranges


def get_sequence_snapshot():
    with sequence_lock:
        return {
            "active": sequence_state["active"],
            "message": sequence_state["message"],
            "steps": [dict(step) for step in sequence_state["steps"]],
            "current_index": sequence_state["current_index"],
            "current_step": dict(sequence_state["current_step"]) if isinstance(sequence_state["current_step"], dict) else None,
            "speeds": dict(sequence_state["speeds"]),
        }


def update_sequence_state(*, active=None, message=None, steps=None, current_index=None, current_step=None, speeds=None):
    with sequence_lock:
        if active is not None:
            sequence_state["active"] = active
        if message is not None:
            sequence_state["message"] = message
        if steps is not None:
            sequence_state["steps"] = steps
        if current_index is not None or current_index is None:
            sequence_state["current_index"] = current_index
        if current_step is not None or current_step is None:
            sequence_state["current_step"] = current_step
        if speeds is not None:
            sequence_state["speeds"] = speeds


def get_cartesian_snapshot():
    with cartesian_lock:
        return {
            "active": cartesian_state["active"],
            "message": cartesian_state["message"],
            "line": dict(cartesian_state["line"]) if isinstance(cartesian_state["line"], dict) else None,
            "current_segment": cartesian_state["current_segment"],
            "segment_count": cartesian_state["segment_count"],
            "max_speeds": dict(cartesian_state["max_speeds"]),
        }


def update_cartesian_state(*, active=_STATE_UNSET, message=_STATE_UNSET, line=_STATE_UNSET, current_segment=_STATE_UNSET, segment_count=_STATE_UNSET, max_speeds=_STATE_UNSET):
    with cartesian_lock:
        if active is not _STATE_UNSET:
            cartesian_state["active"] = active
        if message is not _STATE_UNSET:
            cartesian_state["message"] = message
        if line is not _STATE_UNSET:
            cartesian_state["line"] = line
        if current_segment is not _STATE_UNSET:
            cartesian_state["current_segment"] = current_segment
        if segment_count is not _STATE_UNSET:
            cartesian_state["segment_count"] = segment_count
        if max_speeds is not _STATE_UNSET:
            cartesian_state["max_speeds"] = max_speeds


def get_point_move_snapshot():
    with point_move_lock:
        return {
            "active": point_move_state["active"],
            "message": point_move_state["message"],
            "targets": dict(point_move_state["targets"]) if isinstance(point_move_state["targets"], dict) else None,
            "current_segment": point_move_state["current_segment"],
            "segment_count": point_move_state["segment_count"],
            "max_speeds": dict(point_move_state["max_speeds"]),
        }


def update_point_move_state(*, active=_STATE_UNSET, message=_STATE_UNSET, targets=_STATE_UNSET, current_segment=_STATE_UNSET, segment_count=_STATE_UNSET, max_speeds=_STATE_UNSET):
    with point_move_lock:
        if active is not _STATE_UNSET:
            point_move_state["active"] = active
        if message is not _STATE_UNSET:
            point_move_state["message"] = message
        if targets is not _STATE_UNSET:
            point_move_state["targets"] = targets
        if current_segment is not _STATE_UNSET:
            point_move_state["current_segment"] = current_segment
        if segment_count is not _STATE_UNSET:
            point_move_state["segment_count"] = segment_count
        if max_speeds is not _STATE_UNSET:
            point_move_state["max_speeds"] = max_speeds


def stop_demo_mode(send_stop=True):
    global demo_thread

    thread = None
    was_active = False
    with demo_lock:
        thread = demo_thread
        was_active = demo_state["active"] or (thread is not None and thread.is_alive())
        demo_stop_event.set()

    if thread and thread.is_alive():
        thread.join(timeout=2.0)

    if send_stop and was_active:
        try:
            send_serial_command("STOP ALL", reset_buffer=False)
        except Exception:
            pass

    with demo_lock:
        demo_thread = None
        demo_state["active"] = False
        if send_stop and was_active:
            demo_state["message"] = "Inactive"
        demo_state["axes"] = {}
        demo_state["ranges"] = {}


def stop_sequence_mode(send_stop=True):
    global sequence_thread

    thread = None
    was_active = False
    with sequence_lock:
        thread = sequence_thread
        was_active = sequence_state["active"] or (thread is not None and thread.is_alive())
        sequence_stop_event.set()

    if thread and thread.is_alive():
        thread.join(timeout=2.0)

    if send_stop and was_active:
        try:
            send_serial_command("STOP ALL", reset_buffer=False)
        except Exception:
            pass

    with sequence_lock:
        sequence_thread = None
        sequence_state["active"] = False
        if send_stop and was_active:
            sequence_state["message"] = "Inactive"
        sequence_state["steps"] = []
        sequence_state["current_index"] = None
        sequence_state["current_step"] = None
        sequence_state["speeds"] = {}


def stop_cartesian_mode(send_stop=True):
    global cartesian_thread

    thread = None
    was_active = False
    with cartesian_lock:
        thread = cartesian_thread
        was_active = cartesian_state["active"] or (thread is not None and thread.is_alive())
        cartesian_stop_event.set()

    if thread and thread.is_alive():
        thread.join(timeout=2.0)

    if send_stop and was_active:
        try:
            send_serial_command("STOP ALL", reset_buffer=False)
        except Exception:
            pass

    with cartesian_lock:
        cartesian_thread = None
        cartesian_state["active"] = False
        if send_stop and was_active:
            cartesian_state["message"] = "Inactive"
        cartesian_state["line"] = None
        cartesian_state["current_segment"] = None
        cartesian_state["segment_count"] = 0
        cartesian_state["max_speeds"] = {}


def stop_point_move(send_stop=True):
    global point_move_thread

    thread = None
    was_active = False
    with point_move_lock:
        thread = point_move_thread
        was_active = point_move_state["active"] or (thread is not None and thread.is_alive())
        point_move_stop_event.set()

    if thread and thread.is_alive():
        thread.join(timeout=2.0)

    if send_stop and was_active:
        try:
            send_serial_command("STOP ALL", reset_buffer=False)
        except Exception:
            pass

    with point_move_lock:
        point_move_thread = None
        point_move_state["active"] = False
        if send_stop and was_active:
            point_move_state["message"] = "Inactive"
        point_move_state["targets"] = None
        point_move_state["current_segment"] = None
        point_move_state["segment_count"] = 0
        point_move_state["max_speeds"] = {}


def send_axis_move_to_distance(axis, current_distance, target_distance, speed):
    delta = target_distance - current_distance
    if delta == 0:
        return True

    direction = get_direction_for_distance_delta(axis, delta)
    steps = abs(delta)
    cmd = f"MOVE_STEPS {axis} {steps} {direction} {speed}"
    response = send_serial_command(cmd, reset_buffer=False)
    return response == "OK"


def get_sequence_speeds(raw_speeds):
    raw_speeds = raw_speeds if isinstance(raw_speeds, dict) else {}
    return {
        "X": sanitize_axis_speed(raw_speeds.get("X")) or 200,
        "Y": sanitize_axis_speed(raw_speeds.get("Y")) or 200,
        "Z": sanitize_axis_speed(raw_speeds.get("Z")) or 1500,
    }


def get_cartesian_max_speeds(raw_speeds):
    raw_speeds = raw_speeds if isinstance(raw_speeds, dict) else {}
    return {
        "X": sanitize_axis_speed(raw_speeds.get("X")) or 1200,
        "Y": sanitize_axis_speed(raw_speeds.get("Y")) or 1200,
        "Z": sanitize_axis_speed(raw_speeds.get("Z")) or 1500,
    }


def wait_for_targets(targets, step_name, stop_event, *, require_home=False, poll_interval=SEQUENCE_POLL_INTERVAL, timeout=SEQUENCE_STEP_TIMEOUT):
    started = time.time()

    while not stop_event.is_set():
        all_reached = True

        for axis, target in targets.items():
            current = read_axis_distance(axis)
            if not isinstance(current, int):
                if require_home:
                    all_reached = False
                    continue
                raise RuntimeError(f"{axis} position became unreadable while running {step_name}.")

            if abs(current - target) > SEQUENCE_TARGET_TOLERANCE:
                all_reached = False

        if all_reached:
            return True

        if (time.time() - started) >= timeout:
            raise RuntimeError(f"Timed out while running {step_name}.")

        time.sleep(poll_interval)

    return False


def wait_for_sequence_targets(targets, step_name, *, require_home=False):
    return wait_for_targets(
        targets,
        step_name,
        sequence_stop_event,
        require_home=require_home,
        poll_interval=SEQUENCE_POLL_INTERVAL,
        timeout=SEQUENCE_STEP_TIMEOUT,
    )


def wait_for_cartesian_targets(targets, step_name):
    return wait_for_targets(
        targets,
        step_name,
        cartesian_stop_event,
        require_home=False,
        poll_interval=CARTESIAN_POLL_INTERVAL,
        timeout=CARTESIAN_STEP_TIMEOUT,
    )


def run_sequence_position_step(step, speeds):
    targets = step["positions"]
    current_positions = {axis: read_axis_distance(axis) for axis in DEMO_AXES}

    for axis, current in current_positions.items():
        if not isinstance(current, int):
            raise RuntimeError(f"{axis} must be homed before route step '{step['name']}' can run.")

    coordinated_speeds = compute_coordinated_axis_speeds(current_positions, targets, speeds)
    move_axes_to_targets(
        targets,
        coordinated_speeds,
        step["name"],
        stop_event=sequence_stop_event,
        poll_interval=SEQUENCE_POLL_INTERVAL,
        timeout=SEQUENCE_STEP_TIMEOUT,
    )


def run_sequence_home_all_step(speeds):
    response = send_serial_command(
        f"HOME ALL {speeds['X']} {speeds['Y']} {speeds['Z']}",
        reset_buffer=False,
    )
    if response != "OK":
        raise RuntimeError("Failed to start Home All.")

    wait_for_sequence_targets({"X": 0, "Y": 0, "Z": 0}, "Home All", require_home=True)


def sequence_worker(steps, speeds):
    global sequence_thread

    try:
        update_sequence_state(
            active=True,
            message="Starting route...",
            steps=[dict(step) for step in steps],
            current_index=None,
            current_step=None,
            speeds=dict(speeds),
        )

        for index, step in enumerate(steps):
            if sequence_stop_event.is_set():
                break

            update_sequence_state(
                active=True,
                message=f"Running step {index + 1} of {len(steps)}: {step['name']}",
                current_index=index,
                current_step=dict(step),
            )

            if step["type"] == "position":
                run_sequence_position_step(step, speeds)
            elif step["type"] == "home_all":
                run_sequence_home_all_step(speeds)
            else:
                raise RuntimeError(f"Unsupported route step: {step['type']}")

        if not sequence_stop_event.is_set():
            update_sequence_state(
                active=False,
                message="Route complete.",
                current_index=None,
                current_step=None,
            )
    except Exception as exc:
        try:
            send_serial_command("STOP ALL", reset_buffer=False)
        except Exception:
            pass
        update_sequence_state(
            active=False,
            message=f"Route stopped: {exc}",
            current_index=None,
            current_step=None,
        )
    finally:
        sequence_stop_event.clear()
        with sequence_lock:
            sequence_thread = None


def start_sequence_mode(raw_steps, raw_speeds):
    global sequence_thread

    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("Add at least one route step before starting.")

    stop_demo_mode(send_stop=True)
    stop_sequence_mode(send_stop=True)
    stop_point_move(send_stop=True)

    normalized_steps = []
    for index, raw_step in enumerate(raw_steps, start=1):
        if not isinstance(raw_step, dict):
            raise ValueError("Invalid route step.")

        step_type = str(raw_step.get("type", "")).strip().lower()
        if step_type == "home_all":
            normalized_steps.append({
                "type": "home_all",
                "name": "Home All",
            })
            continue

        if step_type != "position":
            raise ValueError("Route steps must be saved positions or Home All.")

        positions = raw_step.get("positions", {})
        if not isinstance(positions, dict):
            raise ValueError(f"Route step {index} is missing position data.")

        try:
            z_target = int(positions["Z"])
            x_target = int(positions["X"])
            y_target = int(positions["Y"])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"Route step {index} has invalid position coordinates.") from None

        if not (0 <= z_target <= Z_MAX_DISTANCE_FROM_HOME):
            raise ValueError(f"Route step {index} has an invalid base position.")
        if not (0 <= x_target <= X_MAX_DISTANCE_FROM_HOME):
            raise ValueError(f"Route step {index} has an invalid Arm 1 position.")
        if not (0 <= y_target <= Y_MAX_DISTANCE_FROM_HOME):
            raise ValueError(f"Route step {index} has an invalid Arm 2 position.")

        normalized_steps.append({
            "type": "position",
            "name": str(raw_step.get("name") or f"Position {index}").strip() or f"Position {index}",
            "positions": {"Z": z_target, "X": x_target, "Y": y_target},
        })

    speeds = get_sequence_speeds(raw_speeds)
    sequence_stop_event.clear()
    thread = threading.Thread(target=sequence_worker, args=(normalized_steps, speeds), daemon=True)
    with sequence_lock:
        sequence_thread = thread
        sequence_state["active"] = True
        sequence_state["message"] = "Starting route..."
        sequence_state["steps"] = [dict(step) for step in normalized_steps]
        sequence_state["current_index"] = None
        sequence_state["current_step"] = None
        sequence_state["speeds"] = dict(speeds)
    thread.start()


def demo_worker(configs, ranges):
    global demo_thread

    targets = {}
    try:
        update_demo_state(active=True, message="Running", axes=configs, ranges=ranges)

        for axis in DEMO_AXES:
            current = read_axis_distance(axis)
            if not isinstance(current, int):
                raise RuntimeError(f"{axis} is not homed/readable.")

            low_target = ranges[axis]["low"]
            high_target = ranges[axis]["high"]
            initial_target = high_target if abs(high_target - current) >= abs(current - low_target) else low_target
            if initial_target == current:
                initial_target = low_target if high_target == current else high_target

            if initial_target == current:
                raise RuntimeError(f"{axis} has no usable demo travel from its current position.")

            if not send_axis_move_to_distance(axis, current, initial_target, configs[axis]["speed"]):
                raise RuntimeError(f"Failed to start demo motion on {axis}.")

            targets[axis] = initial_target

        while not demo_stop_event.is_set():
            time.sleep(DEMO_POLL_INTERVAL)
            if demo_stop_event.is_set():
                break

            for axis in DEMO_AXES:
                current = read_axis_distance(axis)
                if not isinstance(current, int):
                    raise RuntimeError(f"{axis} position became unreadable during demo mode.")

                target = targets[axis]
                if abs(current - target) > DEMO_TARGET_TOLERANCE:
                    continue

                low_target = ranges[axis]["low"]
                high_target = ranges[axis]["high"]
                next_target = low_target if target == high_target else high_target
                if next_target == current:
                    next_target = high_target if next_target == low_target else low_target

                if next_target == current:
                    continue

                if not send_axis_move_to_distance(axis, current, next_target, configs[axis]["speed"]):
                    raise RuntimeError(f"Failed to continue demo motion on {axis}.")

                targets[axis] = next_target
    except Exception as exc:
        try:
            send_serial_command("STOP ALL", reset_buffer=False)
        except Exception:
            pass
        update_demo_state(active=False, message=f"Demo stopped: {exc}", axes={}, ranges={})
    else:
        update_demo_state(active=False, message="Inactive", axes={}, ranges={})
    finally:
        demo_stop_event.clear()
        with demo_lock:
            demo_thread = None


def start_demo_mode(raw_axes):
    global demo_thread

    if not isinstance(raw_axes, dict):
        raise ValueError("Missing demo axis settings.")

    stop_demo_mode(send_stop=True)
    stop_point_move(send_stop=True)

    configs = {}
    ranges = {}

    for axis in DEMO_AXES:
        raw_config = raw_axes.get(axis, {})
        if not isinstance(raw_config, dict):
            raise ValueError(f"Invalid demo settings for {axis}.")

        speed = sanitize_axis_speed(raw_config.get("speed"))
        steps = sanitize_step_count(raw_config.get("steps"), AXIS_MAX_DISTANCES[axis])
        current = read_axis_distance(axis)

        if speed is None:
            raise ValueError(f"{axis} demo speed is invalid.")
        if steps is None:
            raise ValueError(f"{axis} demo distance is invalid.")
        if not isinstance(current, int):
            raise ValueError(f"{axis} must be homed before demo mode can start.")

        low_target = max(0, current - steps)
        high_target = min(AXIS_MAX_DISTANCES[axis], current + steps)

        if low_target == high_target:
            raise ValueError(f"{axis} is already pinned at its travel limit. Move it away from the limit, then start demo mode.")

        configs[axis] = {"speed": speed, "steps": steps}
        ranges[axis] = {"low": low_target, "high": high_target}

    demo_stop_event.clear()
    thread = threading.Thread(target=demo_worker, args=(configs, ranges), daemon=True)
    with demo_lock:
        demo_thread = thread
        demo_state["active"] = True
        demo_state["message"] = "Starting..."
        demo_state["axes"] = {axis: dict(config) for axis, config in configs.items()}
        demo_state["ranges"] = {axis: dict(bounds) for axis, bounds in ranges.items()}
    thread.start()


def load_saved_positions():
    if not os.path.exists(POSITIONS_FILE):
        return []

    try:
        with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except Exception:
        pass

    return []


def save_saved_positions(positions):
    with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(positions, f, indent=2)


def load_saved_cartesian_lines():
    if not os.path.exists(CARTESIAN_LINES_FILE):
        return []

    try:
        with open(CARTESIAN_LINES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data[:MAX_SAVED_CARTESIAN_LINES]
    except Exception:
        pass

    return []


def save_saved_cartesian_lines(lines):
    with open(CARTESIAN_LINES_FILE, "w", encoding="utf-8") as f:
        json.dump(lines[:MAX_SAVED_CARTESIAN_LINES], f, indent=2)


def get_serial(force_reopen=False):
    global ser

    if force_reopen and ser is not None:
        try:
            if ser.is_open:
                ser.close()
        except Exception:
            pass
        ser = None

    if ser is None or not ser.is_open:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
        time.sleep(2)

    return ser


def send_serial_command(command, reset_buffer=True, retries=2):
    last_error = None

    for attempt in range(retries + 1):
        try:
            with serial_lock:
                s = get_serial(force_reopen=(attempt > 0))
                if reset_buffer:
                    s.reset_input_buffer()
                s.write(f"{command}\n".encode("utf-8"))
                response = s.readline().decode("utf-8", errors="ignore").strip()
                return response
        except SerialException as e:
            last_error = e
            time.sleep(SERIAL_RETRY_DELAY)

    raise last_error


def clamp_value(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def wrap_degrees_360(degrees):
    normalized = degrees % 360.0
    if normalized < 0:
        normalized += 360.0
    return normalized


def normalize_degrees_signed(degrees):
    normalized = degrees % 360.0
    if normalized > 180.0:
        normalized -= 360.0
    if normalized < -180.0:
        normalized += 360.0
    return normalized


def angular_distance_degrees(left, right):
    return abs(normalize_degrees_signed(left - right))


def read_all_axis_positions():
    return {axis: read_axis_distance(axis) for axis in DEMO_AXES}


def forward_kinematics_cartesian(base_deg_from_home, arm1_deg, arm2_relative_deg):
    base_angle = math.radians(BASE_HOME_DEG + base_deg_from_home + ARM_PLANE_OFFSET_DEG)
    arm1_angle = math.radians(arm1_deg)
    arm2_relative = math.radians(arm2_relative_deg)
    arm2_absolute = arm1_angle + arm2_relative
    radial = (ARM1_LENGTH_CM * math.cos(arm1_angle)) + (ARM2_LENGTH_CM * math.cos(arm2_absolute))
    vertical = (ARM1_LENGTH_CM * math.sin(arm1_angle)) + (ARM2_LENGTH_CM * math.sin(arm2_absolute))
    elbow_radial = ARM1_LENGTH_CM * math.cos(arm1_angle)
    elbow_vertical = ARM1_LENGTH_CM * math.sin(arm1_angle)

    return {
        "baseDegFromHome": base_deg_from_home,
        "baseDegSigned": normalize_degrees_signed(base_deg_from_home),
        "arm1Deg": arm1_deg,
        "arm2RelativeDeg": arm2_relative_deg,
        "arm2AbsoluteDeg": arm1_deg + arm2_relative_deg,
        "position": {
            "x": math.cos(base_angle) * radial,
            "y": math.sin(base_angle) * radial,
            "z": BASE_HEIGHT_CM + vertical,
        },
        "elbow": {
            "x": math.cos(base_angle) * elbow_radial,
            "y": math.sin(base_angle) * elbow_radial,
            "z": BASE_HEIGHT_CM + elbow_vertical,
        },
        "radial": radial,
        "vertical": vertical,
    }


def get_current_joint_pose():
    current = read_all_axis_positions()
    if not all(isinstance(current[axis], int) for axis in DEMO_AXES):
        return None

    base_deg_from_home = current["Z"] / BASE_STEPS_PER_DEG
    arm1_deg = ARM1_HOME_DEG + (current["X"] / ARM1_STEPS_PER_DEG)
    arm2_relative_deg = ARM2_HOME_RELATIVE_DEG - (current["Y"] / ARM2_STEPS_PER_DEG)
    fk = forward_kinematics_cartesian(base_deg_from_home, arm1_deg, arm2_relative_deg)

    return {
        "baseDegFromHome": base_deg_from_home,
        "baseDegSigned": normalize_degrees_signed(base_deg_from_home),
        "arm1Deg": arm1_deg,
        "arm2RelativeDeg": arm2_relative_deg,
        "arm2AbsoluteDeg": arm1_deg + arm2_relative_deg,
        "position": dict(fk["position"]),
        "elbow": dict(fk["elbow"]),
    }


def compute_cartesian_candidate(target, elbow_mode, base_offset_deg=0.0):
    target_bearing = math.atan2(target["y"], target["x"])
    base_deg_from_home = wrap_degrees_360(
        math.degrees(target_bearing) - BASE_HOME_DEG - ARM_PLANE_OFFSET_DEG + base_offset_deg
    )
    base_deg_signed = normalize_degrees_signed(base_deg_from_home)
    radial_magnitude = math.hypot(target["x"], target["y"])
    radial = -radial_magnitude if base_offset_deg == 180 else radial_magnitude
    vertical = target["z"] - BASE_HEIGHT_CM
    distance = math.hypot(radial, vertical)
    raw_cos_arm2 = (
        (radial * radial) +
        (vertical * vertical) -
        (ARM1_LENGTH_CM * ARM1_LENGTH_CM) -
        (ARM2_LENGTH_CM * ARM2_LENGTH_CM)
    ) / (2 * ARM1_LENGTH_CM * ARM2_LENGTH_CM)
    clamped_cos_arm2 = clamp_value(raw_cos_arm2, -1.0, 1.0)
    sin_arm2_magnitude = math.sqrt(max(0.0, 1.0 - (clamped_cos_arm2 * clamped_cos_arm2)))
    elbow_sign = 1.0 if elbow_mode == "up" else -1.0
    arm2_relative = math.atan2(elbow_sign * sin_arm2_magnitude, clamped_cos_arm2)
    arm1 = math.atan2(vertical, radial) - math.atan2(
        ARM2_LENGTH_CM * math.sin(arm2_relative),
        ARM1_LENGTH_CM + (ARM2_LENGTH_CM * math.cos(arm2_relative))
    )
    arm1_deg = math.degrees(arm1)
    arm2_relative_deg = math.degrees(arm2_relative)
    arm2_absolute_deg = arm1_deg + arm2_relative_deg
    fk = forward_kinematics_cartesian(base_deg_from_home, arm1_deg, arm2_relative_deg)
    position_error = math.dist(
        (fk["position"]["x"], fk["position"]["y"], fk["position"]["z"]),
        (target["x"], target["y"], target["z"]),
    )

    within_distance = -1.0 <= raw_cos_arm2 <= 1.0
    within_base = BASE_HOME_DEG <= base_deg_from_home <= BASE_TRAVEL_DEG
    within_arm1 = ARM1_HOME_DEG <= arm1_deg <= (ARM1_HOME_DEG + ARM1_TRAVEL_DEG)
    within_arm2 = ARM2_MIN_DEG <= arm2_relative_deg <= ARM2_MAX_DEG
    within_limits = within_base and within_arm1 and within_arm2

    return {
        "target": dict(target),
        "baseOffsetDeg": base_offset_deg,
        "baseDegFromHome": base_deg_from_home,
        "baseDegSigned": base_deg_signed,
        "bearingDegSigned": normalize_degrees_signed(math.degrees(target_bearing) - BASE_HOME_DEG),
        "radial": radial,
        "radialMagnitude": radial_magnitude,
        "vertical": vertical,
        "distance": distance,
        "rawCosArm2": raw_cos_arm2,
        "arm1Deg": arm1_deg,
        "arm2RelativeDeg": arm2_relative_deg,
        "arm2AbsoluteDeg": arm2_absolute_deg,
        "elbow": dict(fk["elbow"]),
        "endPoint": dict(fk["position"]),
        "positionError": position_error,
        "withinDistance": within_distance,
        "withinBase": within_base,
        "withinArm1": within_arm1,
        "withinArm2": within_arm2,
        "withinLimits": within_limits,
        "exactReachable": within_distance and within_limits and position_error <= ARM_POSE_POSITION_TOLERANCE_CM,
    }


def get_candidate_transition_cost(candidate, prior_pose):
    if not isinstance(prior_pose, dict):
        return (
            angular_distance_degrees(candidate["baseDegFromHome"], 0.0) * BASE_BRANCH_WEIGHT +
            abs(candidate["arm1Deg"] - ARM1_HOME_DEG) +
            abs(candidate["arm2RelativeDeg"] - ARM2_HOME_RELATIVE_DEG)
        )

    return (
        angular_distance_degrees(candidate["baseDegFromHome"], prior_pose.get("baseDegFromHome", 0.0)) * BASE_BRANCH_WEIGHT +
        abs(candidate["arm1Deg"] - prior_pose.get("arm1Deg", ARM1_HOME_DEG)) +
        (abs(candidate["arm2RelativeDeg"] - prior_pose.get("arm2RelativeDeg", ARM2_HOME_RELATIVE_DEG)) * 1.25)
    )


def solve_cartesian_target(target, elbow_mode="up", prior_pose=None):
    candidates = [
        compute_cartesian_candidate(target, elbow_mode, 0.0),
        compute_cartesian_candidate(target, elbow_mode, 180.0),
    ]

    def sort_key(candidate):
        limit_penalty = (
            (0 if candidate["withinBase"] else 1) +
            (0 if candidate["withinArm1"] else 1) +
            (0 if candidate["withinArm2"] else 1)
        )
        return (
            limit_penalty,
            0 if candidate["withinDistance"] else 1,
            round(candidate["positionError"], 6),
            round(get_candidate_transition_cost(candidate, prior_pose), 6),
        )

    candidates.sort(key=sort_key)
    return candidates[0]


def candidate_to_axis_positions(candidate):
    positions = {
        "Z": int(round(candidate["baseDegFromHome"] * BASE_STEPS_PER_DEG)),
        "X": int(round((candidate["arm1Deg"] - ARM1_HOME_DEG) * ARM1_STEPS_PER_DEG)),
        "Y": int(round((ARM2_HOME_RELATIVE_DEG - candidate["arm2RelativeDeg"]) * ARM2_STEPS_PER_DEG)),
    }

    if not (0 <= positions["Z"] <= Z_MAX_DISTANCE_FROM_HOME):
        raise ValueError("Base target is outside travel.")
    if not (0 <= positions["X"] <= X_MAX_DISTANCE_FROM_HOME):
        raise ValueError("Arm 1 target is outside travel.")
    if not (0 <= positions["Y"] <= Y_MAX_DISTANCE_FROM_HOME):
        raise ValueError("Arm 2 target is outside travel.")

    return positions


def normalize_cartesian_point(raw_point, label):
    if not isinstance(raw_point, dict):
        raise ValueError(f"{label} point is missing.")

    try:
        x = float(raw_point.get("x"))
        y = float(raw_point.get("y"))
        z = float(raw_point.get("z"))
    except (TypeError, ValueError):
        raise ValueError(f"{label} point must contain numeric X, Y, and Z values.") from None

    if not all(math.isfinite(value) for value in (x, y, z)):
        raise ValueError(f"{label} point contains invalid coordinates.")

    if abs(x) > 80 or abs(y) > 80 or z < 0 or z > 80:
        raise ValueError(f"{label} point is outside the allowed entry range.")

    return {
        "x": round(x, 3),
        "y": round(y, 3),
        "z": round(z, 3),
    }


def normalize_cartesian_line(raw_line):
    if not isinstance(raw_line, dict):
        raise ValueError("Missing line definition.")

    name = str(raw_line.get("name", "")).strip()
    start = normalize_cartesian_point(raw_line.get("start"), "Start")
    end = normalize_cartesian_point(raw_line.get("end"), "End")
    elbow_mode = "down" if str(raw_line.get("elbow_mode", "up")).strip().lower() == "down" else "up"

    try:
        segments = int(raw_line.get("segments", 24))
    except (TypeError, ValueError):
        raise ValueError("Segments must be a whole number.") from None

    segments = max(MIN_CARTESIAN_SEGMENTS, min(MAX_CARTESIAN_SEGMENTS, segments))

    return {
        "name": name or "Cartesian Line",
        "start": start,
        "end": end,
        "elbow_mode": elbow_mode,
        "segments": segments,
    }


def sample_cartesian_line_points(line):
    start = line["start"]
    end = line["end"]
    segment_count = line["segments"]
    points = []

    for index in range(segment_count + 1):
        t = 0.0 if segment_count == 0 else (index / segment_count)
        points.append({
            "x": start["x"] + ((end["x"] - start["x"]) * t),
            "y": start["y"] + ((end["y"] - start["y"]) * t),
            "z": start["z"] + ((end["z"] - start["z"]) * t),
        })

    return points


def compute_segment_speeds(previous_steps, next_steps, max_speeds):
    deltas = {
        axis: abs(next_steps[axis] - previous_steps[axis])
        for axis in DEMO_AXES
    }
    moving = {axis: delta for axis, delta in deltas.items() if delta > 0}
    if not moving:
        return {}

    duration_seconds = max(
        delta / max(max_speeds[axis], MIN_AXIS_SPEED)
        for axis, delta in moving.items()
    )
    duration_seconds = max(duration_seconds, 1.0 / MIN_AXIS_SPEED)

    speeds = {}
    for axis, delta in moving.items():
        requested_speed = math.ceil(delta / duration_seconds)
        speeds[axis] = max(MIN_AXIS_SPEED, min(max_speeds[axis], requested_speed))

    return speeds


def compute_coordinated_axis_speeds(current_positions, target_positions, max_speeds):
    deltas = {
        axis: abs(int(target_positions[axis]) - int(current_positions[axis]))
        for axis in DEMO_AXES
    }
    moving = {axis: delta for axis, delta in deltas.items() if delta > 0}
    if not moving:
        return {}

    duration_seconds = max(
        delta / max(max_speeds[axis], MIN_AXIS_SPEED)
        for axis, delta in moving.items()
    )
    duration_seconds = max(duration_seconds, 1.0 / MIN_AXIS_SPEED)

    coordinated = {}
    for axis, delta in moving.items():
        requested = math.ceil(delta / duration_seconds)
        coordinated[axis] = max(MIN_AXIS_SPEED, min(max_speeds[axis], requested))

    return coordinated


def normalize_axis_targets(raw_positions):
    if not isinstance(raw_positions, dict):
        raise ValueError("Invalid target positions.")

    try:
        targets = {
            "Z": int(raw_positions["Z"]),
            "X": int(raw_positions["X"]),
            "Y": int(raw_positions["Y"]),
        }
    except (KeyError, TypeError, ValueError):
        raise ValueError("Target positions must include integer Z, X, and Y values.") from None

    if not (0 <= targets["Z"] <= Z_MAX_DISTANCE_FROM_HOME):
        raise ValueError("Base target is outside travel.")
    if not (0 <= targets["X"] <= X_MAX_DISTANCE_FROM_HOME):
        raise ValueError("Arm 1 target is outside travel.")
    if not (0 <= targets["Y"] <= Y_MAX_DISTANCE_FROM_HOME):
        raise ValueError("Arm 2 target is outside travel.")

    return targets


def start_point_move(targets, raw_max_speeds, *, hard_stop_previous=False):
    global point_move_thread

    stop_demo_mode(send_stop=True)
    stop_sequence_mode(send_stop=True)
    stop_cartesian_mode(send_stop=True)
    stop_point_move(send_stop=hard_stop_previous)

    current_positions = read_all_axis_positions()
    for axis, current in current_positions.items():
        if not isinstance(current, int):
            raise ValueError(f"{axis} must be homed before moving to a saved point.")

    max_speeds = get_cartesian_max_speeds(raw_max_speeds)
    coordinated_speeds = compute_coordinated_axis_speeds(current_positions, targets, max_speeds)

    if not coordinated_speeds:
        update_point_move_state(
            active=False,
            message="Target already reached.",
            targets=dict(targets),
            current_segment=None,
            segment_count=0,
            max_speeds=dict(max_speeds),
        )
        return {
            "targets": dict(targets),
            "current": dict(current_positions),
            "speeds": {},
            "segment_count": 0,
        }

    point_move_stop_event.clear()
    thread = threading.Thread(target=point_move_worker, args=(dict(targets), dict(coordinated_speeds), dict(max_speeds)), daemon=True)
    with point_move_lock:
        point_move_thread = thread
        point_move_state["active"] = True
        point_move_state["message"] = "Starting point move..."
        point_move_state["targets"] = dict(targets)
        point_move_state["current_segment"] = 1
        point_move_state["segment_count"] = 1
        point_move_state["max_speeds"] = dict(max_speeds)
    thread.start()

    return {
        "targets": dict(targets),
        "current": dict(current_positions),
        "speeds": coordinated_speeds,
        "segment_count": 1,
    }


def move_axes_to_targets(targets, speeds, step_name, *, stop_event=cartesian_stop_event, poll_interval=CARTESIAN_POLL_INTERVAL, timeout=CARTESIAN_STEP_TIMEOUT):
    current_positions = read_all_axis_positions()
    for axis, current in current_positions.items():
        if not isinstance(current, int):
            raise RuntimeError(f"{axis} must be homed before {step_name} can run.")

    started_any = False
    for axis in DEMO_AXES:
        target = targets[axis]
        current = current_positions[axis]
        if target == current:
            continue

        axis_speed = sanitize_axis_speed(speeds.get(axis))
        if axis_speed is None:
            raise RuntimeError(f"{axis} speed is invalid for {step_name}.")

        if not send_axis_move_to_distance(axis, current, target, axis_speed):
            raise RuntimeError(f"Failed to start {step_name} on {axis}.")
        started_any = True

    if started_any:
        wait_for_targets(
            targets,
            step_name,
            stop_event,
            require_home=False,
            poll_interval=poll_interval,
            timeout=timeout,
        )


def point_move_worker(targets, speeds, max_speeds):
    global point_move_thread

    try:
        for axis, current in read_all_axis_positions().items():
            if not isinstance(current, int):
                raise RuntimeError(f"{axis} must be homed before a point move can run.")

        update_point_move_state(
            active=True,
            message="Running point move...",
            targets=dict(targets),
            current_segment=1,
            segment_count=1,
            max_speeds=dict(max_speeds),
        )

        move_axes_to_targets(
            targets,
            speeds,
            "Point move",
            stop_event=point_move_stop_event,
            poll_interval=POINT_MOVE_POLL_INTERVAL,
            timeout=POINT_MOVE_STEP_TIMEOUT,
        )

        if not point_move_stop_event.is_set():
            update_point_move_state(
                active=False,
                message="Point move complete.",
                targets=dict(targets),
                current_segment=None,
                segment_count=1,
                max_speeds=dict(max_speeds),
            )
    except Exception as exc:
        try:
            send_serial_command("STOP ALL", reset_buffer=False)
        except Exception:
            pass
        update_point_move_state(
            active=False,
            message=f"Point move stopped: {exc}",
            targets=dict(targets) if isinstance(targets, dict) else None,
            current_segment=None,
            segment_count=0,
            max_speeds=dict(max_speeds) if isinstance(max_speeds, dict) else {},
        )
    finally:
        point_move_stop_event.clear()
        with point_move_lock:
            point_move_thread = None


def validate_cartesian_line(line, prior_pose=None):
    solved_points = []
    current_prior = prior_pose

    for index, point in enumerate(sample_cartesian_line_points(line), start=1):
        candidate = solve_cartesian_target(point, line["elbow_mode"], prior_pose=current_prior)
        if not candidate["exactReachable"]:
            raise ValueError(
                f"Point {index} on '{line['name']}' is outside the travel-limited workspace: "
                f"({point['x']:.2f}, {point['y']:.2f}, {point['z']:.2f})"
            )

        solved_points.append({
            "point": point,
            "candidate": candidate,
            "steps": candidate_to_axis_positions(candidate),
        })
        current_prior = candidate

    return solved_points


def cartesian_worker(line, max_speeds):
    global cartesian_thread

    try:
        current_pose = get_current_joint_pose()
        if current_pose is None:
            raise RuntimeError("Base, Arm 1, and Arm 2 must all be homed before running a Cartesian line.")

        solved_points = validate_cartesian_line(line, prior_pose=current_pose)
        total_segments = max(0, len(solved_points) - 1)
        update_cartesian_state(
            active=True,
            message=f"Moving to the start of '{line['name']}'...",
            line=dict(line),
            current_segment=0,
            segment_count=total_segments,
            max_speeds=dict(max_speeds),
        )

        move_axes_to_targets(solved_points[0]["steps"], max_speeds, f"{line['name']} start move")
        previous_steps = solved_points[0]["steps"]

        for segment_index, solved in enumerate(solved_points[1:], start=1):
            if cartesian_stop_event.is_set():
                break

            segment_speeds = compute_segment_speeds(previous_steps, solved["steps"], max_speeds)
            update_cartesian_state(
                active=True,
                message=f"Running '{line['name']}' segment {segment_index} of {total_segments}.",
                line=dict(line),
                current_segment=segment_index,
                segment_count=total_segments,
                max_speeds=dict(max_speeds),
            )

            if segment_speeds:
                move_axes_to_targets(solved["steps"], segment_speeds, f"{line['name']} segment {segment_index}")
            previous_steps = solved["steps"]

        if not cartesian_stop_event.is_set():
            update_cartesian_state(
                active=False,
                message=f"Cartesian line complete: {line['name']}",
                line=dict(line),
                current_segment=None,
                segment_count=total_segments,
                max_speeds=dict(max_speeds),
            )
    except Exception as exc:
        try:
            send_serial_command("STOP ALL", reset_buffer=False)
        except Exception:
            pass
        update_cartesian_state(
            active=False,
            message=f"Cartesian line stopped: {exc}",
            line=dict(line) if isinstance(line, dict) else None,
            current_segment=None,
            segment_count=line.get("segments", 0) if isinstance(line, dict) else 0,
            max_speeds=dict(max_speeds) if isinstance(max_speeds, dict) else {},
        )
    finally:
        cartesian_stop_event.clear()
        with cartesian_lock:
            cartesian_thread = None


def start_cartesian_mode(raw_line, raw_max_speeds):
    global cartesian_thread

    line = normalize_cartesian_line(raw_line)
    max_speeds = get_cartesian_max_speeds(raw_max_speeds)

    stop_demo_mode(send_stop=True)
    stop_sequence_mode(send_stop=True)
    stop_cartesian_mode(send_stop=True)
    stop_point_move(send_stop=True)

    current_pose = get_current_joint_pose()
    if current_pose is None:
        raise ValueError("Base, Arm 1, and Arm 2 must all be homed before running a Cartesian line.")
    validate_cartesian_line(line, prior_pose=current_pose)

    cartesian_stop_event.clear()
    thread = threading.Thread(target=cartesian_worker, args=(line, max_speeds), daemon=True)
    with cartesian_lock:
        cartesian_thread = thread
        cartesian_state["active"] = True
        cartesian_state["message"] = f"Preparing '{line['name']}'..."
        cartesian_state["line"] = dict(line)
        cartesian_state["current_segment"] = 0
        cartesian_state["segment_count"] = line["segments"]
        cartesian_state["max_speeds"] = dict(max_speeds)
    thread.start()

    return line


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/ik-sandbox")
def ik_sandbox():
    return redirect("/")


@app.route("/move", methods=["POST"])
def move():
    stop_demo_mode(send_stop=True)
    stop_sequence_mode(send_stop=True)
    stop_cartesian_mode(send_stop=True)
    stop_point_move(send_stop=True)
    data = request.get_json()
    axis = data["axis"].upper()
    speed = int(data["speed"])
    direction = int(data["direction"])
    duration = int(data["duration"])

    cmd = f"MOVE {axis} {speed} {direction} {duration}"
    response = send_serial_command(cmd, reset_buffer=False)

    return jsonify({"ok": response == "OK", "sent": cmd, "raw": response})


@app.route("/move_steps", methods=["POST"])
def move_steps():
    stop_demo_mode(send_stop=True)
    stop_sequence_mode(send_stop=True)
    stop_cartesian_mode(send_stop=True)
    stop_point_move(send_stop=True)
    data = request.get_json()
    axis = data["axis"].upper()
    speed = int(data["speed"])
    direction = int(data["direction"])
    steps = int(data["steps"])

    cmd = f"MOVE_STEPS {axis} {steps} {direction} {speed}"
    response = send_serial_command(cmd, reset_buffer=False)

    return jsonify({"ok": response == "OK", "sent": cmd, "raw": response})


@app.route("/goto_position", methods=["POST"])
def goto_position():
    data = request.get_json(silent=True) or {}

    try:
        targets = normalize_axis_targets(data.get("positions", {}))
        result = start_point_move(targets, data.get("max_speeds", {}), hard_stop_previous=False)
    except (ValueError, RuntimeError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    return jsonify({
        "ok": True,
        "message": "Point move started.",
        "targets": result["targets"],
        "current": result["current"],
        "speeds": result["speeds"],
    })


@app.route("/stop", methods=["POST"])
def stop():
    stop_demo_mode(send_stop=True)
    stop_sequence_mode(send_stop=True)
    stop_cartesian_mode(send_stop=True)
    stop_point_move(send_stop=True)
    data = request.get_json()
    axis = data.get("axis", "ALL").upper()

    cmd = f"STOP {axis}"
    response = send_serial_command(cmd, reset_buffer=False)

    return jsonify({"ok": response == "OK", "sent": cmd, "raw": response})


@app.route("/status", methods=["GET"])
def status():
    response = send_serial_command("STATUS")

    limit_z = False
    if response.startswith("LIMIT_Z="):
        value = response.split("=")[1]
        limit_z = (value == "1")

    return jsonify({
        "limit_z": limit_z,
        "raw": response
    })


@app.route("/limits", methods=["GET"])
def limits():
    response = send_serial_command("LIMITS")

    result = {
        "limit_x": "INACTIVE",
        "limit_y": "INACTIVE",
        "limit_z": False,
        "limit_a": "INACTIVE",
        "raw": response,
    }

    for part in response.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip()

        if key == "LIMIT_X":
            result["limit_x"] = (value == "1")
        elif key == "LIMIT_Y":
            result["limit_y"] = (value == "1")
        elif key == "LIMIT_Z":
            result["limit_z"] = (value == "1")
        elif key == "LIMIT_A":
            result["limit_a"] = value

    return jsonify(result)


@app.route("/telemetry", methods=["GET"])
def telemetry():
    limits_response = send_serial_command("LIMITS")
    distance_response = send_serial_command("DIST Z")
    distance_y_response = send_serial_command("DIST Y")
    distance_x_response = send_serial_command("DIST X")
    demo = get_demo_snapshot()
    sequence = get_sequence_snapshot()
    cartesian = get_cartesian_snapshot()
    point_move = get_point_move_snapshot()

    result = {
        "limit_x": False,
        "limit_y": False,
        "limit_z": False,
        "limit_a": "INACTIVE",
        "distance_x": None,
        "distance_y": None,
        "distance_z": None,
        "x_homed": False,
        "x_at_home": False,
        "x_at_max_distance": False,
        "x_can_move_forward": False,
        "x_can_move_reverse": True,
        "x_max_distance": X_MAX_DISTANCE_FROM_HOME,
        "y_homed": False,
        "y_at_home": False,
        "y_at_max_distance": False,
        "y_can_move_forward": True,
        "y_can_move_reverse": False,
        "y_max_distance": Y_MAX_DISTANCE_FROM_HOME,
        "z_homed": False,
        "z_at_home": False,
        "z_at_max_distance": False,
        "z_can_move_forward": True,
        "z_can_move_reverse": False,
        "z_max_distance": Z_MAX_DISTANCE_FROM_HOME,
        "demo": demo,
        "sequence": sequence,
        "cartesian": cartesian,
        "point_move": point_move,
        "limits_raw": limits_response,
        "distance_x_raw": distance_x_response,
        "distance_y_raw": distance_y_response,
        "distance_raw": distance_response,
    }

    for part in limits_response.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip()

        if key == "LIMIT_X":
            result["limit_x"] = (value == "1")
        elif key == "LIMIT_Y":
            result["limit_y"] = (value == "1")
        elif key == "LIMIT_Z":
            result["limit_z"] = (value == "1")
        elif key == "LIMIT_A":
            result["limit_a"] = value

    if distance_y_response.startswith("DIST_Y="):
        value = distance_y_response.split("=", 1)[1]
        if value == "UNKNOWN":
            result["distance_y"] = "UNKNOWN"
        else:
            try:
                result["distance_y"] = int(value)
            except ValueError:
                result["distance_y"] = None

    if distance_x_response.startswith("DIST_X="):
        value = distance_x_response.split("=", 1)[1]
        if value == "UNKNOWN":
            result["distance_x"] = "UNKNOWN"
        else:
            try:
                result["distance_x"] = int(value)
            except ValueError:
                result["distance_x"] = None

    if distance_response.startswith("DIST_Z="):
        value = distance_response.split("=", 1)[1]
        if value == "UNKNOWN":
            result["distance_z"] = "UNKNOWN"
        else:
            try:
                result["distance_z"] = int(value)
            except ValueError:
                result["distance_z"] = None

    distance_x = result["distance_x"]
    if isinstance(distance_x, int):
        result["x_homed"] = True
        result["x_at_home"] = (distance_x == 0)
        result["x_at_max_distance"] = (distance_x >= X_MAX_DISTANCE_FROM_HOME)
        if result["x_at_home"]:
            result["x_can_move_forward"] = True
            result["x_can_move_reverse"] = False
        elif result["x_at_max_distance"]:
            result["x_can_move_forward"] = False
            result["x_can_move_reverse"] = True
        else:
            result["x_can_move_forward"] = True
            result["x_can_move_reverse"] = True
    else:
        result["x_homed"] = False
        result["x_at_home"] = False
        result["x_at_max_distance"] = False
        result["x_can_move_forward"] = False
        result["x_can_move_reverse"] = True

    distance_y = result["distance_y"]
    if isinstance(distance_y, int):
        result["y_homed"] = True
        result["y_at_home"] = (distance_y == 0)
        result["y_at_max_distance"] = (distance_y >= Y_MAX_DISTANCE_FROM_HOME)
        if result["y_at_home"]:
            result["y_can_move_forward"] = False
            result["y_can_move_reverse"] = True
        elif result["y_at_max_distance"]:
            result["y_can_move_forward"] = True
            result["y_can_move_reverse"] = False
        else:
            result["y_can_move_forward"] = True
            result["y_can_move_reverse"] = True
    else:
        result["y_homed"] = False
        result["y_at_home"] = False
        result["y_at_max_distance"] = False
        result["y_can_move_forward"] = True
        result["y_can_move_reverse"] = False

    distance_z = result["distance_z"]
    if isinstance(distance_z, int):
        result["z_homed"] = True
        result["z_at_home"] = (distance_z == 0)
        result["z_at_max_distance"] = (distance_z >= Z_MAX_DISTANCE_FROM_HOME)
        result["z_can_move_forward"] = not result["z_at_home"]
        result["z_can_move_reverse"] = not result["z_at_max_distance"]
    else:
        result["z_homed"] = False
        result["z_at_home"] = False
        result["z_at_max_distance"] = False
        result["z_can_move_forward"] = True
        result["z_can_move_reverse"] = False

    return jsonify(result)


@app.route("/distance", methods=["GET"])
def distance():
    axis = request.args.get("axis", "Z").upper()
    response = send_serial_command(f"DIST {axis}")

    distance_value = None
    if axis == "Z" and response.startswith("DIST_Z="):
        value = response.split("=", 1)[1]
        if value == "UNKNOWN":
            distance_value = "UNKNOWN"
        else:
            try:
                distance_value = int(value)
            except ValueError:
                distance_value = None
    elif axis == "Y" and response.startswith("DIST_Y="):
        value = response.split("=", 1)[1]
        if value == "UNKNOWN":
            distance_value = "UNKNOWN"
        else:
            try:
                distance_value = int(value)
            except ValueError:
                distance_value = None
    elif axis == "X" and response.startswith("DIST_X="):
        value = response.split("=", 1)[1]
        if value == "UNKNOWN":
            distance_value = "UNKNOWN"
        else:
            try:
                distance_value = int(value)
            except ValueError:
                distance_value = None

    if axis == "Z":
        z_homed = isinstance(distance_value, int)
        z_at_home = z_homed and distance_value == 0
        z_at_max_distance = z_homed and distance_value >= Z_MAX_DISTANCE_FROM_HOME

        return jsonify({
            "axis": axis,
            "distance_z": distance_value,
            "z_homed": z_homed,
            "z_at_home": z_at_home,
            "z_at_max_distance": z_at_max_distance,
            "z_max_distance": Z_MAX_DISTANCE_FROM_HOME,
            "raw": response,
        })

    if axis == "Y":
        y_homed = isinstance(distance_value, int)
        y_at_home = y_homed and distance_value == 0
        y_at_max_distance = y_homed and distance_value >= Y_MAX_DISTANCE_FROM_HOME

        return jsonify({
            "axis": axis,
            "distance_y": distance_value,
            "y_homed": y_homed,
            "y_at_home": y_at_home,
            "y_at_max_distance": y_at_max_distance,
            "y_max_distance": Y_MAX_DISTANCE_FROM_HOME,
            "raw": response,
        })

    x_homed = isinstance(distance_value, int)
    x_at_home = x_homed and distance_value == 0
    x_at_max_distance = x_homed and distance_value >= X_MAX_DISTANCE_FROM_HOME

    return jsonify({
        "axis": axis,
        "distance_x": distance_value,
        "x_homed": x_homed,
        "x_at_home": x_at_home,
        "x_at_max_distance": x_at_max_distance,
        "x_max_distance": X_MAX_DISTANCE_FROM_HOME,
        "raw": response,
    })


@app.route("/home", methods=["POST"])
def home():
    stop_demo_mode(send_stop=True)
    stop_sequence_mode(send_stop=True)
    stop_cartesian_mode(send_stop=True)
    stop_point_move(send_stop=True)
    data = request.get_json(silent=True) or {}
    axis = data.get("axis", "Z").upper()
    speed = sanitize_axis_speed(data.get("speed"))

    if axis == "Z":
        speed = ROTATING_BASE_HOME_SPEED

    cmd = f"HOME {axis}" if speed is None else f"HOME {axis} {speed}"
    response = send_serial_command(cmd, reset_buffer=False)

    return jsonify({
        "ok": response == "OK",
        "sent": cmd,
        "raw": response,
    })


@app.route("/home_all", methods=["POST"])
def home_all():
    stop_demo_mode(send_stop=True)
    stop_sequence_mode(send_stop=True)
    stop_cartesian_mode(send_stop=True)
    stop_point_move(send_stop=True)
    data = request.get_json(silent=True) or {}
    speeds = data.get("speeds", {}) if isinstance(data.get("speeds", {}), dict) else {}

    speed_x = sanitize_axis_speed(speeds.get("X"))
    speed_y = sanitize_axis_speed(speeds.get("Y"))
    speed_z = ROTATING_BASE_HOME_SPEED

    if all(speed is not None for speed in [speed_x, speed_y, speed_z]):
        cmd = f"HOME ALL {speed_x} {speed_y} {speed_z}"
    else:
        cmd = "HOME ALL"
    response = send_serial_command(cmd, reset_buffer=False)

    return jsonify({
        "ok": response == "OK",
        "sent": cmd,
        "raw": response,
    })


@app.route("/demo/start", methods=["POST"])
def demo_start():
    stop_cartesian_mode(send_stop=True)
    stop_sequence_mode(send_stop=True)
    data = request.get_json(silent=True) or {}
    try:
        start_demo_mode(data.get("axes", {}))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc), "demo": get_demo_snapshot()}), 400

    return jsonify({"ok": True, "message": "Demo mode started.", "demo": get_demo_snapshot()})


@app.route("/demo/stop", methods=["POST"])
def demo_stop():
    stop_demo_mode(send_stop=True)
    return jsonify({"ok": True, "message": "Demo mode stopped.", "demo": get_demo_snapshot()})


@app.route("/sequence/start", methods=["POST"])
def sequence_start():
    stop_cartesian_mode(send_stop=True)
    data = request.get_json(silent=True) or {}
    try:
        start_sequence_mode(data.get("steps", []), data.get("speeds", {}))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc), "sequence": get_sequence_snapshot()}), 400

    return jsonify({
        "ok": True,
        "message": "Route started.",
        "sequence": get_sequence_snapshot(),
    })


@app.route("/sequence/stop", methods=["POST"])
def sequence_stop():
    stop_sequence_mode(send_stop=True)
    return jsonify({
        "ok": True,
        "message": "Route stopped.",
        "sequence": get_sequence_snapshot(),
    })


@app.route("/cartesian-lines", methods=["GET"])
def get_cartesian_lines():
    with cartesian_lines_lock:
        lines = load_saved_cartesian_lines()
    return jsonify({"lines": lines})


@app.route("/cartesian-lines", methods=["POST"])
def save_cartesian_line():
    data = request.get_json(silent=True) or {}

    try:
        line = normalize_cartesian_line(data)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    with cartesian_lines_lock:
        saved = load_saved_cartesian_lines()
        replaced = False

        for index, existing in enumerate(saved):
            if existing.get("name") == line["name"]:
                saved[index] = line
                replaced = True
                break

        if not replaced:
            if len(saved) >= MAX_SAVED_CARTESIAN_LINES:
                return jsonify({"ok": False, "error": f"Only {MAX_SAVED_CARTESIAN_LINES} saved Cartesian lines are allowed."}), 400
            saved.append(line)

        save_saved_cartesian_lines(saved)

    return jsonify({"ok": True, "line": line})


@app.route("/cartesian-lines/<int:index>", methods=["DELETE"])
def delete_cartesian_line(index):
    with cartesian_lines_lock:
        saved = load_saved_cartesian_lines()
        if index < 0 or index >= len(saved):
            return jsonify({"ok": False, "error": "Cartesian line not found."}), 404
        removed = saved.pop(index)
        save_saved_cartesian_lines(saved)

    return jsonify({"ok": True, "deleted": removed})


@app.route("/cartesian-line/start", methods=["POST"])
def cartesian_start():
    data = request.get_json(silent=True) or {}
    try:
        line = start_cartesian_mode(data.get("line", {}), data.get("max_speeds", {}))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc), "cartesian": get_cartesian_snapshot()}), 400

    return jsonify({
        "ok": True,
        "message": f"Cartesian line started: {line['name']}",
        "cartesian": get_cartesian_snapshot(),
    })


@app.route("/cartesian-line/stop", methods=["POST"])
def cartesian_stop():
    stop_cartesian_mode(send_stop=True)
    return jsonify({
        "ok": True,
        "message": "Cartesian line stopped.",
        "cartesian": get_cartesian_snapshot(),
    })


@app.route("/positions", methods=["GET"])
def get_positions():
    with positions_lock:
        positions = load_saved_positions()
    return jsonify({"positions": positions})


@app.route("/positions", methods=["POST"])
def save_position():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    positions = data.get("positions", {})

    try:
        base = positions.get("Z")
        arm1 = positions.get("X")
        arm2 = positions.get("Y")
        if not all(isinstance(v, int) for v in [base, arm1, arm2]):
            raise ValueError
    except Exception:
        return jsonify({"ok": False, "error": "Invalid position data."}), 400

    if not name:
        with positions_lock:
            existing = load_saved_positions()
            name = f"Position {len(existing) + 1}"

    with positions_lock:
        saved = load_saved_positions()
        entry = {"name": name, "positions": {"Z": base, "X": arm1, "Y": arm2}}

        replaced = False
        for index, existing in enumerate(saved):
            if existing.get("name") == name:
                saved[index] = entry
                replaced = True
                break

        if not replaced:
            saved.append(entry)

        save_saved_positions(saved)

    return jsonify({"ok": True, "position": entry})


@app.route("/positions/<int:index>", methods=["DELETE"])
def delete_position(index):
    with positions_lock:
        saved = load_saved_positions()
        if index < 0 or index >= len(saved):
            return jsonify({"ok": False, "error": "Position not found."}), 404
        removed = saved.pop(index)
        save_saved_positions(saved)

    return jsonify({"ok": True, "deleted": removed})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
