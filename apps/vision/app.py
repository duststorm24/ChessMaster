from __future__ import annotations

import csv
import io
import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from flask import Flask, Response, jsonify, render_template_string, request, send_from_directory

try:
    from picamera2 import Picamera2
except Exception as exc:  # pragma: no cover - only happens off the Pi or with broken camera libs.
    Picamera2 = None
    PICAMERA_IMPORT_ERROR = str(exc)
else:
    PICAMERA_IMPORT_ERROR = None

try:
    from libcamera import controls as libcamera_controls
except Exception as exc:  # pragma: no cover - only happens off the Pi.
    libcamera_controls = None
    LIBCAMERA_CONTROLS_IMPORT_ERROR = str(exc)
else:
    LIBCAMERA_CONTROLS_IMPORT_ERROR = None


APP_DIR = Path(__file__).resolve().parent
CAPTURE_DIR = Path(os.environ.get("CAPTURE_DIR", APP_DIR / "captures"))
SQUARE_CROPS_DIR = CAPTURE_DIR / "square-crops"
BOARD_DETECTION_DIR = CAPTURE_DIR / "board-detections"
CALIBRATION_PATH = Path(os.environ.get("CALIBRATION_PATH", APP_DIR / "calibration.json"))
POSITION_PATH = Path(os.environ.get("POSITION_PATH", APP_DIR / "position.json"))
OCCUPANCY_MODEL_PATH = Path(os.environ.get("OCCUPANCY_MODEL_PATH", APP_DIR / "occupancy_model.json"))
PIECE_MODEL_PATH = Path(os.environ.get("PIECE_MODEL_PATH", APP_DIR / "piece_model.json"))
CAMERA_SETTINGS_PATH = Path(os.environ.get("CAMERA_SETTINGS_PATH", APP_DIR / "camera_settings.json"))
DEFAULT_FRAME_WIDTH = int(os.environ.get("FRAME_WIDTH", "1280"))
DEFAULT_FRAME_HEIGHT = int(os.environ.get("FRAME_HEIGHT", "720"))
DEFAULT_JPEG_QUALITY = int(os.environ.get("JPEG_QUALITY", "85"))
DEFAULT_STREAM_FPS = float(os.environ.get("STREAM_FPS", "12"))
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
DEFAULT_CALIBRATION = {"left": 26.5, "top": 8.0, "size": 47.25, "white_bottom": True}
FILES = "abcdefgh"
OCCUPANCY_FEATURES = ("mean_brightness", "contrast", "edge_density")
PIECE_IMAGE_SIZE = 16
DATASET_CSV_COLUMNS = (
    "source",
    "square",
    "piece",
    "occupied",
    "url",
    "fen",
    "mean_brightness",
    "contrast",
    "edge_density",
)
PIECE_CODES = {
    "wK": "K",
    "wQ": "Q",
    "wR": "R",
    "wB": "B",
    "wN": "N",
    "wP": "P",
    "bK": "k",
    "bQ": "q",
    "bR": "r",
    "bB": "b",
    "bN": "n",
    "bP": "p",
}

CAMERA_MODES = {
    "hd": {
        "id": "hd",
        "label": "HD 16:9",
        "resolution": [DEFAULT_FRAME_WIDTH, DEFAULT_FRAME_HEIGHT],
        "fps": DEFAULT_STREAM_FPS,
        "fov": "cropped",
        "description": "Fast 16:9 preview",
    },
    "wide": {
        "id": "wide",
        "label": "Full FOV",
        "resolution": [2328, 1748],
        "fps": 2.0,
        "fov": "full",
        "description": "Full sensor view",
    },
    "max": {
        "id": "max",
        "label": "Max FOV",
        "resolution": [4656, 3496],
        "fps": 1.0,
        "fov": "full",
        "description": "Highest resolution",
    },
}
DEFAULT_CAMERA_MODE_ID = os.environ.get("CAMERA_MODE", "hd")
FOCUS_MODES = {"continuous", "auto", "manual"}
AF_RANGES = {"normal", "macro", "full"}
AF_SPEEDS = {"normal", "fast"}
COLOR_ORDERS = {"rgb", "bgr"}
EXPOSURE_MODES = {"auto", "manual"}


def clamp_int(value, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, int(value)))


def clamp_float(value, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, float(value)))


def camera_mode(mode_id: str | None) -> dict:
    return CAMERA_MODES.get(str(mode_id or ""), CAMERA_MODES.get(DEFAULT_CAMERA_MODE_ID, CAMERA_MODES["hd"]))


def normalize_camera_settings(data: dict | None = None) -> dict:
    if data is not None and not isinstance(data, dict):
        raise ValueError("Camera settings must be a JSON object.")

    source = data or {}
    default_mode = camera_mode(DEFAULT_CAMERA_MODE_ID)
    mode = camera_mode(source.get("mode_id", default_mode["id"]))
    focus_mode = str(source.get("focus_mode", "continuous")).lower()
    af_range = str(source.get("af_range", "full")).lower()
    af_speed = str(source.get("af_speed", "normal")).lower()
    color_order = str(source.get("color_order", "rgb")).lower()
    exposure_mode = str(source.get("exposure_mode", "auto")).lower()

    if focus_mode not in FOCUS_MODES:
        focus_mode = "continuous"
    if af_range not in AF_RANGES:
        af_range = "full"
    if af_speed not in AF_SPEEDS:
        af_speed = "normal"
    if color_order not in COLOR_ORDERS:
        color_order = "rgb"
    if exposure_mode not in EXPOSURE_MODES:
        exposure_mode = "auto"

    try:
        lens_position = clamp_float(source.get("lens_position", 1.0), 0.0, 32.0)
    except (TypeError, ValueError):
        lens_position = 1.0

    try:
        jpeg_quality = clamp_int(source.get("jpeg_quality", DEFAULT_JPEG_QUALITY), 40, 95)
    except (TypeError, ValueError):
        jpeg_quality = DEFAULT_JPEG_QUALITY

    def normalized_float(name: str, fallback: float, minimum: float, maximum: float) -> float:
        try:
            return clamp_float(source.get(name, fallback), minimum, maximum)
        except (TypeError, ValueError):
            return fallback

    exposure_time_us = int(round(normalized_float("exposure_time_us", 30000.0, 100.0, 250000.0)))
    analogue_gain = normalized_float("analogue_gain", 2.0, 1.0, 16.0)
    exposure_value = normalized_float("exposure_value", 0.0, -4.0, 4.0)
    brightness = normalized_float("brightness", 0.0, -1.0, 1.0)
    contrast = normalized_float("contrast", 1.0, 0.0, 4.0)
    saturation = normalized_float("saturation", 1.0, 0.0, 4.0)
    sharpness = normalized_float("sharpness", 1.0, 0.0, 16.0)
    shadow_lift = normalized_float("shadow_lift", 0.0, 0.0, 1.0)
    purple_fix = normalized_float("purple_fix", 0.0, 0.0, 1.0)

    return {
        "mode_id": mode["id"],
        "mode_label": mode["label"],
        "resolution": mode["resolution"],
        "stream_fps": mode["fps"],
        "fov": mode["fov"],
        "description": mode["description"],
        "focus_mode": focus_mode,
        "af_range": af_range,
        "af_speed": af_speed,
        "lens_position": round(lens_position, 2),
        "color_order": color_order,
        "exposure_mode": exposure_mode,
        "exposure_time_us": exposure_time_us,
        "analogue_gain": round(analogue_gain, 2),
        "exposure_value": round(exposure_value, 2),
        "brightness": round(brightness, 2),
        "contrast": round(contrast, 2),
        "saturation": round(saturation, 2),
        "sharpness": round(sharpness, 2),
        "shadow_lift": round(shadow_lift, 2),
        "purple_fix": round(purple_fix, 2),
        "jpeg_quality": jpeg_quality,
    }


def load_camera_settings() -> dict:
    if not CAMERA_SETTINGS_PATH.exists():
        return normalize_camera_settings()
    try:
        return normalize_camera_settings(json.loads(CAMERA_SETTINGS_PATH.read_text()))
    except (OSError, ValueError, TypeError):
        return normalize_camera_settings()


def save_camera_settings(data: dict) -> dict:
    current = load_camera_settings()
    merged = {**current, **(data or {})}
    settings = normalize_camera_settings(merged)
    CAMERA_SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n")
    return settings


def active_camera_settings() -> dict:
    manager = globals().get("camera_manager")
    if manager is not None:
        return manager.settings()
    return load_camera_settings()


def active_frame_size() -> tuple[int, int]:
    width, height = active_camera_settings()["resolution"]
    return int(width), int(height)


def active_image_aspect() -> float:
    width, height = active_frame_size()
    return width / height


def control_enum(enum_name: str, member_name: str, fallback: int):
    if libcamera_controls is None:
        return fallback
    enum = getattr(libcamera_controls, enum_name, None)
    if enum is None:
        return fallback
    return getattr(enum, member_name, fallback)


def find_v4l2_focus_device() -> str | None:
    for path in sorted(Path("/dev").glob("v4l-subdev*")):
        result = run_command(["v4l2-ctl", "-d", str(path), "--list-ctrls"], timeout=2.0)
        if "focus_absolute" in result.get("output", ""):
            return str(path)
    return None


def v4l2_focus_absolute() -> dict:
    device = find_v4l2_focus_device()
    if device is None:
        return {"available": False, "device": None, "value": None}

    result = run_command(["v4l2-ctl", "-d", device, "--get-ctrl=focus_absolute"], timeout=2.0)
    value = None
    output = result.get("output") or ""
    if ":" in output:
        try:
            value = int(output.rsplit(":", 1)[1].strip())
        except ValueError:
            value = None
    return {"available": result["ok"], "device": device, "value": value, "output": output}


def set_v4l2_focus_absolute(lens_position: float) -> dict:
    device = find_v4l2_focus_device()
    if device is None:
        return {"available": False, "device": None, "value": None, "ok": False}

    value = clamp_int(round((clamp_float(lens_position, 0.0, 32.0) / 32.0) * 4095), 0, 4095)
    result = run_command(["v4l2-ctl", "-d", device, "-c", f"focus_absolute={value}"], timeout=2.0)
    current = v4l2_focus_absolute()
    return {
        "available": True,
        "device": device,
        "value": current.get("value", value),
        "requested_value": value,
        "ok": result["ok"],
        "output": result.get("output"),
    }


def focus_algorithm_available(camera_properties: dict | None = None) -> bool:
    model = str((camera_properties or {}).get("Model") or "").lower()
    candidates = []
    if model:
        candidates.extend(
            [
                Path(f"/usr/share/libcamera/ipa/rpi/pisp/{model}.json"),
                Path(f"/usr/share/libcamera/ipa/rpi/vc4/{model}.json"),
            ]
        )
    candidates.extend(
        [
            Path("/usr/share/libcamera/ipa/rpi/pisp/imx519.json"),
            Path("/usr/share/libcamera/ipa/rpi/vc4/imx519.json"),
        ]
    )
    for path in candidates:
        if path.exists():
            try:
                text = path.read_text()
            except OSError:
                continue
            if '"rpi.af"' in text or '"rpi.focus"' in text:
                return True
    return False

app = Flask(__name__)


class CameraManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._camera = None
        self._settings = load_camera_settings()
        self._sensor_modes = []
        self._focus_controls = {}
        self._camera_properties = {}
        self._focus_status = {"last_error": None, "last_updated_at": None}
        self._last_error: Optional[str] = None
        self._last_frame_at: Optional[float] = None
        self._last_frame = None
        self._next_retry_at = 0.0

    def settings(self) -> dict:
        with self._lock:
            return dict(self._settings)

    def frame_size(self) -> tuple[int, int]:
        settings = self.settings()
        width, height = settings["resolution"]
        return int(width), int(height)

    def _close_locked(self) -> None:
        if self._camera is None:
            return
        camera = self._camera
        self._camera = None
        try:
            camera.close()
        except Exception:
            pass

    def _focus_control_payload(self, camera) -> dict:
        payload = {}
        controls = getattr(camera, "camera_controls", {}) or {}
        for name in ("AfMode", "AfTrigger", "AfRange", "AfSpeed", "LensPosition", "ScalerCrop"):
            if name not in controls:
                continue
            value = controls[name]
            try:
                payload[name] = list(value) if isinstance(value, tuple) else value
            except TypeError:
                payload[name] = str(value)
        return payload

    def _sensor_mode_payload(self, camera) -> list[dict]:
        modes = []
        for mode in getattr(camera, "sensor_modes", []) or []:
            size = mode.get("size")
            crop_limits = mode.get("crop_limits")
            modes.append(
                {
                    "size": list(size) if size else None,
                    "fps": mode.get("fps"),
                    "crop_limits": list(crop_limits) if crop_limits else None,
                    "format": str(mode.get("format")),
                }
            )
        return modes

    def _apply_camera_controls_locked(self, camera) -> None:
        settings = self._settings
        controls = {}
        frame_duration = int(1_000_000 / max(float(settings["stream_fps"]), 0.1))
        if settings["exposure_mode"] == "manual":
            frame_duration = max(frame_duration, int(settings["exposure_time_us"]) + 1_000)
        camera_controls = getattr(camera, "camera_controls", {}) or {}
        has_focus_algorithm = focus_algorithm_available(self._camera_properties)

        if "FrameDurationLimits" in camera_controls:
            controls["FrameDurationLimits"] = (frame_duration, frame_duration)

        if "AeEnable" in camera_controls:
            controls["AeEnable"] = settings["exposure_mode"] == "auto"

        if settings["exposure_mode"] == "manual":
            if "ExposureTime" in camera_controls:
                controls["ExposureTime"] = int(settings["exposure_time_us"])
            if "AnalogueGain" in camera_controls:
                controls["AnalogueGain"] = float(settings["analogue_gain"])
        elif "ExposureValue" in camera_controls:
            controls["ExposureValue"] = float(settings["exposure_value"])

        for setting_name, control_name in (
            ("brightness", "Brightness"),
            ("contrast", "Contrast"),
            ("saturation", "Saturation"),
            ("sharpness", "Sharpness"),
        ):
            if control_name in camera_controls:
                controls[control_name] = float(settings[setting_name])

        if has_focus_algorithm and "AfRange" in camera_controls:
            range_name = {"normal": "Normal", "macro": "Macro", "full": "Full"}[settings["af_range"]]
            controls["AfRange"] = control_enum("AfRangeEnum", range_name, {"normal": 0, "macro": 1, "full": 2}[settings["af_range"]])

        if has_focus_algorithm and "AfSpeed" in camera_controls:
            speed_name = {"normal": "Normal", "fast": "Fast"}[settings["af_speed"]]
            controls["AfSpeed"] = control_enum("AfSpeedEnum", speed_name, {"normal": 0, "fast": 1}[settings["af_speed"]])

        if has_focus_algorithm and "AfMode" in camera_controls:
            mode_name = {
                "manual": "Manual",
                "auto": "Auto",
                "continuous": "Continuous",
            }[settings["focus_mode"]]
            controls["AfMode"] = control_enum("AfModeEnum", mode_name, {"manual": 0, "auto": 1, "continuous": 2}[settings["focus_mode"]])

        if has_focus_algorithm and settings["focus_mode"] == "manual" and "LensPosition" in camera_controls:
            controls["LensPosition"] = float(settings["lens_position"])

        if not controls:
            if settings["focus_mode"] == "manual":
                self._focus_status = {
                    "last_error": None,
                    "last_updated_at": datetime.now().isoformat(timespec="seconds"),
                    "v4l2_focus": set_v4l2_focus_absolute(settings["lens_position"]),
                    "libcamera_focus_algorithm": False,
                }
            return

        try:
            camera.set_controls(controls)
            v4l2_focus = None
            if settings["focus_mode"] == "manual":
                v4l2_focus = set_v4l2_focus_absolute(settings["lens_position"])
            self._focus_status = {
                "last_error": None,
                "last_updated_at": datetime.now().isoformat(timespec="seconds"),
                "v4l2_focus": v4l2_focus,
                "libcamera_focus_algorithm": focus_algorithm_available(self._camera_properties),
            }
        except Exception as exc:
            self._focus_status = {
                "last_error": camera_error_message(exc),
                "last_updated_at": datetime.now().isoformat(timespec="seconds"),
            }

    def _focus_controls_locked(self, trigger: bool = False) -> dict:
        if self._camera is None:
            return {}

        settings = self._settings
        camera_controls = getattr(self._camera, "camera_controls", {}) or {}
        controls = {}
        if not focus_algorithm_available(self._camera_properties):
            return controls

        if "AfRange" in camera_controls:
            range_name = {"normal": "Normal", "macro": "Macro", "full": "Full"}[settings["af_range"]]
            controls["AfRange"] = control_enum("AfRangeEnum", range_name, {"normal": 0, "macro": 1, "full": 2}[settings["af_range"]])

        if "AfSpeed" in camera_controls:
            speed_name = {"normal": "Normal", "fast": "Fast"}[settings["af_speed"]]
            controls["AfSpeed"] = control_enum("AfSpeedEnum", speed_name, {"normal": 0, "fast": 1}[settings["af_speed"]])

        if "AfMode" in camera_controls:
            mode_name = {
                "manual": "Manual",
                "auto": "Auto",
                "continuous": "Continuous",
            }[settings["focus_mode"]]
            controls["AfMode"] = control_enum("AfModeEnum", mode_name, {"manual": 0, "auto": 1, "continuous": 2}[settings["focus_mode"]])

        if settings["focus_mode"] == "manual" and "LensPosition" in camera_controls:
            controls["LensPosition"] = float(settings["lens_position"])

        if trigger and "AfTrigger" in camera_controls:
            controls["AfTrigger"] = control_enum("AfTriggerEnum", "Start", 0)

        return controls

    def start(self) -> bool:
        with self._lock:
            if self._camera is not None:
                return True

            now = time.monotonic()
            if now < self._next_retry_at:
                return False

            if Picamera2 is None:
                self._last_error = f"Picamera2 unavailable: {PICAMERA_IMPORT_ERROR}"
                self._next_retry_at = now + 5
                return False

            camera = None
            try:
                camera = Picamera2()
                self._camera_properties = dict(getattr(camera, "camera_properties", {}) or {})
                self._sensor_modes = self._sensor_mode_payload(camera)
                self._focus_controls = self._focus_control_payload(camera)
                width, height = self._settings["resolution"]
                config = camera.create_video_configuration(
                    main={"size": (int(width), int(height)), "format": "RGB888"}
                )
                camera.configure(config)
                self._apply_camera_controls_locked(camera)
                camera.start()
                time.sleep(1.0)
            except Exception as exc:
                self._last_error = camera_error_message(exc)
                self._next_retry_at = now + 5
                if camera is not None:
                    try:
                        camera.close()
                    except Exception:
                        pass
                return False

            self._camera = camera
            self._last_error = None
            return True

    def update_settings(self, data: dict) -> dict:
        old_settings = self.settings()
        settings = save_camera_settings(data)
        with self._lock:
            self._settings = settings
            self._next_retry_at = 0.0
            self._last_error = None
            if settings["mode_id"] != old_settings.get("mode_id"):
                self._last_frame = None
                self._last_frame_at = None
                self._close_locked()
            elif self._camera is not None:
                self._apply_camera_controls_locked(self._camera)
        return settings

    def update_focus(self, data: dict) -> dict:
        focus_data = {
            key: value
            for key, value in (data or {}).items()
            if key in {"focus_mode", "af_range", "af_speed", "lens_position"}
        }
        trigger = bool((data or {}).get("trigger"))
        settings = save_camera_settings({**self.settings(), **focus_data})

        with self._lock:
            self._settings = settings

        if not self.start():
            return {"ok": False, "error": self._last_error or "Camera is not available yet."}

        with self._lock:
            if settings["focus_mode"] == "auto":
                trigger = True
            controls = self._focus_controls_locked(trigger=trigger)
            v4l2_focus = None
            if not controls and settings["focus_mode"] != "manual":
                return {"ok": False, "error": "Focus controls are not available for this camera."}

            try:
                if controls:
                    self._camera.set_controls(controls)
            except Exception as exc:
                self._focus_status = {
                    "last_error": camera_error_message(exc),
                    "last_updated_at": datetime.now().isoformat(timespec="seconds"),
                }
                return {"ok": False, "error": self._focus_status["last_error"]}

            if settings["focus_mode"] == "manual":
                v4l2_focus = set_v4l2_focus_absolute(settings["lens_position"])

            self._focus_status = {
                "last_error": None,
                "last_updated_at": datetime.now().isoformat(timespec="seconds"),
                "v4l2_focus": v4l2_focus,
                "libcamera_focus_algorithm": focus_algorithm_available(self._camera_properties),
            }
            return {"ok": True, "settings": dict(self._settings), "focus_status": dict(self._focus_status)}

    def trigger_autofocus(self) -> dict:
        if not self.start():
            return {"ok": False, "error": self._last_error or "Camera is not available yet."}
        if not focus_algorithm_available(self._camera_properties):
            return {
                "ok": False,
                "error": "Autofocus is not available with the current IMX519 libcamera tuning file. Use Manual focus instead.",
                "settings": dict(self._settings),
                "focus_status": dict(self._focus_status),
            }
        return self.update_focus({"focus_mode": "auto", "trigger": True})

    def capture_rgb(self):
        if not self.start():
            return None

        with self._lock:
            try:
                frame = self._camera.capture_array()
            except Exception as exc:
                self._last_error = camera_error_message(exc)
                failed_camera = self._camera
                self._camera = None
                self._next_retry_at = time.monotonic() + 5
                try:
                    failed_camera.close()
                except Exception:
                    pass
                return None

            self._last_frame = frame
            self._last_frame_at = time.time()
            return frame

    def capture_hdr_rgb(self) -> dict | None:
        if not self.start():
            return None

        with self._lock:
            if self._camera is None:
                return None

            camera = self._camera
            settings = dict(self._settings)
            camera_controls = getattr(camera, "camera_controls", {}) or {}
            base_metadata = {}
            try:
                base_metadata = camera.capture_metadata() or {}
            except Exception:
                base_metadata = {}

            base_exposure = int(base_metadata.get("ExposureTime") or settings["exposure_time_us"] or 30_000)
            base_gain = float(base_metadata.get("AnalogueGain") or settings["analogue_gain"] or 2.0)
            frame_duration_floor = int(1_000_000 / max(float(settings["stream_fps"]), 0.1))
            stack_plan = (
                ("short", 0.35),
                ("base", 1.0),
                ("long", 3.0),
            )
            frames = []
            exposures = []

            try:
                for label, factor in stack_plan:
                    exposure_time = clamp_int(round(base_exposure * factor), 100, 250_000)
                    frame_duration = max(frame_duration_floor, exposure_time + 1_000)
                    controls = {}
                    if "FrameDurationLimits" in camera_controls:
                        controls["FrameDurationLimits"] = (frame_duration, frame_duration)
                    if "AeEnable" in camera_controls:
                        controls["AeEnable"] = False
                    if "ExposureTime" in camera_controls:
                        controls["ExposureTime"] = exposure_time
                    if "AnalogueGain" in camera_controls:
                        controls["AnalogueGain"] = base_gain

                    if controls:
                        camera.set_controls(controls)
                    time.sleep(min(1.4, max(0.28, (exposure_time / 1_000_000) * 2.5)))
                    frame = camera.capture_array()
                    frames.append(frame)
                    exposures.append(
                        {
                            "label": label,
                            "exposure_time_us": exposure_time,
                            "analogue_gain": round(base_gain, 3),
                        }
                    )
            except Exception as exc:
                self._last_error = camera_error_message(exc)
                failed_camera = self._camera
                self._camera = None
                self._next_retry_at = time.monotonic() + 5
                try:
                    failed_camera.close()
                except Exception:
                    pass
                return None
            finally:
                if self._camera is camera:
                    try:
                        self._apply_camera_controls_locked(camera)
                    except Exception:
                        pass

            merged_display = merge_hdr_display_frames(
                [frame_to_display_rgb(frame, settings) for frame in frames]
            )
            merged_frame = display_rgb_to_frame_order(merged_display, settings)
            self._last_frame = merged_frame
            self._last_frame_at = time.time()
            return {
                "frame": merged_frame,
                "metadata": {
                    "capture_mode": "hdr",
                    "base_metadata": base_metadata,
                    "exposures": exposures,
                },
            }

    def status(self) -> dict:
        self.start()
        settings = self.settings()
        return {
            "camera_available": self._camera is not None,
            "last_error": self._last_error,
            "last_frame_at": self._last_frame_at,
            "resolution": settings["resolution"],
            "stream_fps": settings["stream_fps"],
            "mode_id": settings["mode_id"],
            "mode_label": settings["mode_label"],
            "fov": settings["fov"],
            "focus_mode": settings["focus_mode"],
            "af_range": settings["af_range"],
            "af_speed": settings["af_speed"],
            "lens_position": settings["lens_position"],
            "color_order": settings["color_order"],
            "exposure_mode": settings["exposure_mode"],
            "exposure_time_us": settings["exposure_time_us"],
            "analogue_gain": settings["analogue_gain"],
            "exposure_value": settings["exposure_value"],
            "brightness": settings["brightness"],
            "contrast": settings["contrast"],
            "saturation": settings["saturation"],
            "sharpness": settings["sharpness"],
            "shadow_lift": settings["shadow_lift"],
            "purple_fix": settings["purple_fix"],
            "jpeg_quality": settings["jpeg_quality"],
            "camera_properties": dict(self._camera_properties),
            "camera_modes": list(CAMERA_MODES.values()),
            "sensor_modes": self._sensor_modes,
            "focus_controls": self._focus_controls,
            "focus_algorithm_available": focus_algorithm_available(self._camera_properties),
            "v4l2_focus": v4l2_focus_absolute(),
            "focus_status": dict(self._focus_status),
            "captures_dir": str(CAPTURE_DIR),
            "calibration": load_calibration(),
        }


camera_manager = CameraManager()


def camera_error_message(exc: Exception) -> str:
    raw = str(exc) or exc.__class__.__name__
    if "list index out of range" in raw:
        return (
            "No CSI camera was detected. Connect the module to CAM/DISP0, "
            "reboot the Pi, then refresh this page."
        )
    return raw


def run_command(args: list[str], timeout: float = 5.0) -> dict:
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return {"ok": False, "returncode": None, "output": f"{args[0]} not found"}
    except subprocess.TimeoutExpired as exc:
        output = "\n".join(part for part in [exc.stdout, exc.stderr] if part)
        return {"ok": False, "returncode": None, "output": output or "command timed out"}

    output = "\n".join(part.strip() for part in [result.stdout, result.stderr] if part.strip())
    return {"ok": result.returncode == 0, "returncode": result.returncode, "output": output}


def camera_config_lines() -> list[str]:
    lines: list[str] = []
    for path in (Path("/boot/firmware/config.txt"), Path("/boot/config.txt")):
        if not path.exists() or not path.is_file():
            continue
        try:
            for index, line in enumerate(path.read_text().splitlines(), start=1):
                lowered = line.lower()
                if any(
                    token in lowered
                    for token in ("camera", "dtoverlay", "imx", "ov", "start_x")
                ):
                    lines.append(f"{path}:{index}: {line}")
        except OSError as exc:
            lines.append(f"{path}: {exc}")
    return lines


def camera_diagnostics() -> dict:
    config_lines = camera_config_lines()
    return {
        "health": camera_manager.status(),
        "rpicam": run_command(["rpicam-hello", "--list-cameras"]),
        "config_lines": config_lines,
        "config_recommendation": camera_config_recommendation(config_lines),
    }


def camera_config_recommendation(config_lines: list[str]) -> dict:
    joined = "\n".join(config_lines)
    auto_detect_disabled = "camera_auto_detect=0" in joined
    forced_sensor_overlay = any(
        "dtoverlay=imx" in line.lower() or "dtoverlay=ov" in line.lower()
        for line in config_lines
    )
    if auto_detect_disabled and forced_sensor_overlay:
        return {
            "level": "warning",
            "message": (
                "Camera auto-detection is disabled and a sensor overlay is forced. "
                "This is OK only if your module matches the forced overlay; otherwise "
                "run ./set_camera_autodetect.sh after plugging in the camera, then reboot."
            ),
            "recommended_action": "./set_camera_autodetect.sh",
        }
    if auto_detect_disabled:
        return {
            "level": "warning",
            "message": "Camera auto-detection is disabled. Enable it if the connected CSI module is not detected.",
            "recommended_action": "./set_camera_autodetect.sh",
        }
    return {
        "level": "ok",
        "message": "Camera boot config does not show an obvious auto-detect conflict.",
        "recommended_action": None,
    }


def normalize_calibration(data: dict | None, image_aspect: float | None = None) -> dict:
    if data is not None and not isinstance(data, dict):
        raise ValueError("Calibration must be a JSON object.")
    source = data or {}
    aspect = image_aspect or active_image_aspect()
    try:
        left = clamp_float(source.get("left", DEFAULT_CALIBRATION["left"]), 0.0, 95.0)
        top = clamp_float(source.get("top", DEFAULT_CALIBRATION["top"]), 0.0, 95.0)
        size = clamp_float(
            source.get("size", DEFAULT_CALIBRATION["size"]),
            5.0,
            min(95.0, 100.0 / aspect),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Calibration values must be numbers.") from exc
    white_bottom = source.get("white_bottom", DEFAULT_CALIBRATION["white_bottom"])
    if isinstance(white_bottom, str):
        white_bottom = white_bottom.strip().lower() in {"1", "true", "yes", "on"}
    else:
        white_bottom = bool(white_bottom)
    left = min(left, 100.0 - size)
    top = min(top, 100.0 - (size * aspect))
    return {
        "left": round(left, 2),
        "top": round(top, 2),
        "size": round(size, 2),
        "white_bottom": white_bottom,
    }


def load_calibration() -> dict:
    if not CALIBRATION_PATH.exists():
        return DEFAULT_CALIBRATION.copy()
    try:
        return normalize_calibration(json.loads(CALIBRATION_PATH.read_text()))
    except (OSError, ValueError, TypeError):
        return DEFAULT_CALIBRATION.copy()


def save_calibration(data: dict) -> dict:
    calibration = normalize_calibration(data)
    CALIBRATION_PATH.write_text(json.dumps(calibration, indent=2) + "\n")
    return calibration


def square_names() -> list[str]:
    return [f"{file_name}{rank}" for rank in range(1, 9) for file_name in FILES]


def normalize_position(data: dict | None) -> dict:
    if data is not None and not isinstance(data, dict):
        raise ValueError("Position must be a JSON object.")

    source = data or {}
    pieces = source.get("pieces", source)
    if not isinstance(pieces, dict):
        raise ValueError("Position pieces must be a JSON object.")

    valid_squares = set(square_names())
    normalized = {}
    for square, piece in pieces.items():
        square = str(square)
        piece = str(piece)
        if square not in valid_squares:
            raise ValueError(f"Invalid square: {square}")
        if piece == "":
            continue
        if piece not in PIECE_CODES:
            raise ValueError(f"Invalid piece code: {piece}")
        normalized[square] = piece
    return dict(sorted(normalized.items()))


def starting_position() -> dict:
    pieces = {}
    back_rank = ["R", "N", "B", "Q", "K", "B", "N", "R"]
    for index, file_name in enumerate(FILES):
        pieces[f"{file_name}1"] = f"w{back_rank[index]}"
        pieces[f"{file_name}2"] = "wP"
        pieces[f"{file_name}7"] = "bP"
        pieces[f"{file_name}8"] = f"b{back_rank[index]}"
    return pieces


def load_position() -> dict:
    if not POSITION_PATH.exists():
        return {}
    try:
        return normalize_position(json.loads(POSITION_PATH.read_text()))
    except (OSError, ValueError, TypeError):
        return {}


def save_position(data: dict) -> dict:
    position = normalize_position(data)
    POSITION_PATH.write_text(json.dumps({"pieces": position}, indent=2) + "\n")
    return position


def position_to_fen(position: dict | None = None) -> str:
    pieces = normalize_position(position or {})
    ranks = []
    for rank in range(8, 0, -1):
        empty = 0
        rank_text = ""
        for file_name in FILES:
            piece = pieces.get(f"{file_name}{rank}")
            if piece is None:
                empty += 1
                continue
            if empty:
                rank_text += str(empty)
                empty = 0
            rank_text += PIECE_CODES[piece]
        if empty:
            rank_text += str(empty)
        ranks.append(rank_text)
    return f"{'/'.join(ranks)} w - - 0 1"


def position_payload(position: dict | None = None) -> dict:
    pieces = normalize_position(position if position is not None else load_position())
    return {"ok": True, "pieces": pieces, "fen": position_to_fen(pieces)}


def capture_metadata(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        return None


def capture_entries(limit: int = 50) -> list[dict]:
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    entries = []
    for image_path in sorted(CAPTURE_DIR.glob("*.jpg"), key=lambda path: path.stat().st_mtime, reverse=True):
        metadata_path = image_path.with_suffix(".json")
        crop_manifest_path = SQUARE_CROPS_DIR / image_path.stem / "manifest.json"
        crop_metadata = capture_metadata(crop_manifest_path)
        metadata = capture_metadata(metadata_path)
        board_detection = ((metadata or {}).get("board_detection") or {})
        entries.append(
            {
                "filename": image_path.name,
                "url": f"/captures/{image_path.name}",
                "metadata_filename": metadata_path.name if metadata_path.exists() else None,
                "metadata_url": f"/captures/{metadata_path.name}" if metadata_path.exists() else None,
                "created_at": datetime.fromtimestamp(image_path.stat().st_mtime).isoformat(timespec="seconds"),
                "size_bytes": image_path.stat().st_size,
                "fen": (metadata or {}).get("fen"),
                "pieces": (metadata or {}).get("pieces", {}),
                "labels_updated_at": (metadata or {}).get("labels_updated_at"),
                "calibration": (metadata or {}).get("calibration"),
                "square_crops_url": f"/captures/square-crops/{image_path.stem}/manifest.json"
                if crop_manifest_path.exists()
                else None,
                "square_crops_count": len((crop_metadata or {}).get("crops", [])),
                "square_analysis_at": (crop_metadata or {}).get("analysis_created_at"),
                "occupancy_predictions_at": (crop_metadata or {}).get("occupancy_predictions_at"),
                "piece_predictions_at": (crop_metadata or {}).get("piece_predictions_at"),
                "predicted_pieces": (crop_metadata or {}).get("predicted_pieces", {}),
                "predicted_piece_count": len((crop_metadata or {}).get("predicted_pieces", {})),
                "predicted_fen": (crop_metadata or {}).get("predicted_fen"),
                "position_recognized_at": (crop_metadata or {}).get("position_recognized_at"),
                "recognition_average_confidence": ((crop_metadata or {}).get("recognition_summary") or {}).get(
                    "average_confidence"
                ),
                "recognition_low_confidence_count": ((crop_metadata or {}).get("recognition_summary") or {}).get(
                    "low_confidence_count"
                ),
                "board_detected_at": board_detection.get("detected_at"),
                "board_detection_method": board_detection.get("method"),
                "board_detection_debug_url": board_detection.get("debug_url"),
            }
        )
        if len(entries) >= limit:
            break
    return entries


def export_square_crops(filename: str) -> dict:
    image_path = (CAPTURE_DIR / filename).resolve()
    capture_root = CAPTURE_DIR.resolve()
    if image_path.parent != capture_root or image_path.suffix.lower() != ".jpg":
        raise ValueError("Square crops can only be exported from root capture JPG files.")
    if not image_path.exists():
        raise FileNotFoundError(filename)

    image = cv2.imread(str(image_path))
    if image is None:
        raise ValueError("OpenCV could not read the capture image.")

    metadata = capture_metadata(image_path.with_suffix(".json")) or {}
    calibration = metadata.get("calibration") or load_calibration()
    squares = metadata.get("squares") or board_squares(calibration)
    pieces = metadata.get("pieces") or {}
    fen = metadata.get("fen") or position_to_fen(pieces)
    height, width = image.shape[:2]
    output_dir = SQUARE_CROPS_DIR / image_path.stem
    output_dir.mkdir(parents=True, exist_ok=True)
    crops = []

    for square in squares:
        rect = square.get("rect_px", {})
        x = max(0, min(width - 1, int(round(float(rect.get("x", 0))))))
        y = max(0, min(height - 1, int(round(float(rect.get("y", 0))))))
        crop_width = max(1, int(round(float(rect.get("width", 1)))))
        crop_height = max(1, int(round(float(rect.get("height", 1)))))
        x2 = max(x + 1, min(width, x + crop_width))
        y2 = max(y + 1, min(height, y + crop_height))
        square_name = square.get("square")
        if square_name not in set(square_names()):
            continue

        crop_path = output_dir / f"{square_name}.jpg"
        ok = cv2.imwrite(str(crop_path), image[y:y2, x:x2])
        if not ok:
            raise RuntimeError(f"Failed to write crop for {square_name}.")

        crops.append(
            {
                "square": square_name,
                "piece": pieces.get(square_name),
                "filename": crop_path.name,
                "url": f"/captures/square-crops/{image_path.stem}/{crop_path.name}",
                "rect_px": {"x": x, "y": y, "width": x2 - x, "height": y2 - y},
            }
        )

    manifest = {
        "source": image_path.name,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "resolution": [width, height],
        "calibration": calibration,
        "fen": fen,
        "pieces": pieces,
        "crops": crops,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    manifest["manifest_url"] = f"/captures/square-crops/{image_path.stem}/manifest.json"
    return manifest


def square_crop_manifest_path(filename: str) -> Path:
    image_path = (CAPTURE_DIR / filename).resolve()
    capture_root = CAPTURE_DIR.resolve()
    if image_path.parent != capture_root or image_path.suffix.lower() != ".jpg":
        raise ValueError("Square crop analysis requires a root capture JPG file.")
    return SQUARE_CROPS_DIR / image_path.stem / "manifest.json"


def analyze_crop_image(path: Path) -> dict:
    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise ValueError(f"OpenCV could not read crop image {path.name}.")

    edges = cv2.Canny(image, 50, 150)
    return {
        "mean_brightness": round(float(np.mean(image)), 2),
        "contrast": round(float(np.std(image)), 2),
        "edge_density": round(float(np.count_nonzero(edges)) / float(edges.size), 4),
    }


def analyze_square_crops(filename: str) -> dict:
    manifest_path = square_crop_manifest_path(filename)
    if not manifest_path.exists():
        manifest = export_square_crops(filename)
    else:
        manifest = capture_metadata(manifest_path) or {}

    crop_root = manifest_path.parent
    analyzed = []
    for crop in manifest.get("crops", []):
        crop_path = crop_root / crop.get("filename", "")
        stats = analyze_crop_image(crop_path)
        crop["analysis"] = {
            **stats,
            "manual_piece": crop.get("piece"),
            "manual_occupied": bool(crop.get("piece")),
        }
        analyzed.append(crop)

    manifest["crops"] = analyzed
    manifest["analysis_created_at"] = datetime.now().isoformat(timespec="seconds")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    manifest["manifest_url"] = f"/captures/square-crops/{Path(filename).stem}/manifest.json"
    return manifest


def update_capture_labels(filename: str, data: dict) -> dict:
    image_path = root_capture_image_path(filename)
    pieces = normalize_position(data)
    metadata_path = image_path.with_suffix(".json")
    metadata = capture_metadata(metadata_path) or {}
    calibration = metadata.get("calibration") or load_calibration()
    squares = metadata.get("squares") or board_squares(calibration)
    fen = position_to_fen(pieces)
    metadata.update(
        {
            "image": metadata.get("image") or image_path.name,
            "calibration": calibration,
            "squares": squares,
            "pieces": pieces,
            "fen": fen,
            "labels_updated_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")

    manifest = None
    manifest_path = SQUARE_CROPS_DIR / image_path.stem / "manifest.json"
    if manifest_path.exists():
        manifest = capture_metadata(manifest_path) or {}
        for crop in manifest.get("crops", []):
            piece = pieces.get(crop.get("square"))
            crop["piece"] = piece
            analysis = crop.get("analysis")
            if analysis:
                analysis["manual_piece"] = piece
                analysis["manual_occupied"] = bool(piece)
        manifest["pieces"] = pieces
        manifest["fen"] = fen
        manifest["labels_updated_at"] = metadata["labels_updated_at"]
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        manifest["manifest_url"] = f"/captures/square-crops/{image_path.stem}/manifest.json"

    return {
        "metadata": metadata,
        "metadata_path": str(metadata_path),
        "manifest": manifest,
        "pieces": pieces,
        "fen": fen,
    }


def save_labeled_sample(filename: str, data: dict) -> dict:
    label_result = update_capture_labels(filename, data)
    export_square_crops(filename)
    manifest = analyze_square_crops(filename)
    labels_updated_at = label_result["metadata"].get("labels_updated_at")
    if labels_updated_at:
        manifest["labels_updated_at"] = labels_updated_at
        manifest_path = square_crop_manifest_path(filename)
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        manifest["manifest_url"] = f"/captures/square-crops/{Path(filename).stem}/manifest.json"
    return {
        "metadata": label_result["metadata"],
        "metadata_path": label_result["metadata_path"],
        "manifest": manifest,
        "pieces": label_result["pieces"],
        "fen": label_result["fen"],
    }


def root_capture_image_path(filename: str) -> Path:
    image_path = (CAPTURE_DIR / filename).resolve()
    capture_root = CAPTURE_DIR.resolve()
    if image_path.parent != capture_root or image_path.suffix.lower() != ".jpg":
        raise ValueError("This action can only use root capture JPG files.")
    if not image_path.exists():
        raise FileNotFoundError(filename)
    return image_path


def delete_capture_artifacts(filename: str) -> dict:
    image_path = root_capture_image_path(filename)
    stem = image_path.stem
    deleted = []

    for path in (
        image_path,
        image_path.with_suffix(".json"),
        BOARD_DETECTION_DIR / image_path.name,
        SQUARE_CROPS_DIR / stem,
    ):
        if not path.exists():
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        deleted.append(str(path))

    return {"filename": image_path.name, "deleted": deleted}


def delete_all_capture_artifacts() -> dict:
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    deleted = []

    for image_path in sorted(CAPTURE_DIR.glob("*.jpg")):
        try:
            result = delete_capture_artifacts(image_path.name)
        except (FileNotFoundError, ValueError):
            continue
        deleted.extend(result["deleted"])

    for metadata_path in sorted(CAPTURE_DIR.glob("*.json")):
        if metadata_path.name in {CAMERA_SETTINGS_PATH.name, CALIBRATION_PATH.name, POSITION_PATH.name}:
            continue
        metadata_path.unlink()
        deleted.append(str(metadata_path))

    for directory in (BOARD_DETECTION_DIR, SQUARE_CROPS_DIR):
        if directory.exists():
            shutil.rmtree(directory)
            deleted.append(str(directory))

    return {"deleted": deleted, "deleted_count": len(deleted)}


def detect_chessboard_corners(gray_image) -> tuple[np.ndarray, str]:
    pattern_size = (7, 7)
    if hasattr(cv2, "findChessboardCornersSB"):
        flags = (
            getattr(cv2, "CALIB_CB_NORMALIZE_IMAGE", 0)
            | getattr(cv2, "CALIB_CB_EXHAUSTIVE", 0)
            | getattr(cv2, "CALIB_CB_ACCURACY", 0)
        )
        found, corners = cv2.findChessboardCornersSB(gray_image, pattern_size, flags)
        if found:
            return corners.reshape(-1, 2).astype(np.float32), "findChessboardCornersSB"

    flags = cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE
    found, corners = cv2.findChessboardCorners(gray_image, pattern_size, flags)
    if found:
        criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)
        refined = cv2.cornerSubPix(gray_image, corners, (7, 7), (-1, -1), criteria)
        return refined.reshape(-1, 2).astype(np.float32), "findChessboardCorners"

    raise ValueError(
        "Could not find the board grid. Try an empty board, brighter light, and a straighter camera angle."
    )


def detected_calibration_from_corners(corners: np.ndarray, width: int, height: int, white_bottom: bool) -> tuple[dict, dict]:
    grid_points = np.array(
        [[col + 1, row + 1] for row in range(7) for col in range(7)],
        dtype=np.float32,
    )
    homography, _ = cv2.findHomography(grid_points, corners, 0)
    if homography is None:
        raise ValueError("OpenCV found board corners but could not estimate the board shape.")

    outer_grid = np.array([[[0, 0], [8, 0], [8, 8], [0, 8]]], dtype=np.float32)
    outer = cv2.perspectiveTransform(outer_grid, homography)[0]
    projected = cv2.perspectiveTransform(np.array([grid_points], dtype=np.float32), homography)[0]
    reprojection_error = float(np.mean(np.linalg.norm(projected - corners, axis=1)))

    edge_lengths = [
        float(np.linalg.norm(outer[1] - outer[0])),
        float(np.linalg.norm(outer[2] - outer[1])),
        float(np.linalg.norm(outer[3] - outer[2])),
        float(np.linalg.norm(outer[0] - outer[3])),
    ]
    board_px = sum(edge_lengths) / len(edge_lengths)
    center_x = float(np.mean(outer[:, 0]))
    center_y = float(np.mean(outer[:, 1]))
    top_edge = outer[1] - outer[0]
    rotation_degrees = float(np.degrees(np.arctan2(top_edge[1], top_edge[0])))

    calibration = normalize_calibration(
        {
            "left": ((center_x - (board_px / 2.0)) / float(width)) * 100.0,
            "top": ((center_y - (board_px / 2.0)) / float(height)) * 100.0,
            "size": (board_px / float(width)) * 100.0,
            "white_bottom": white_bottom,
        }
    )
    detection = {
        "detected_at": datetime.now().isoformat(timespec="seconds"),
        "method": "opencv_chessboard_7x7",
        "reprojection_error_px": round(reprojection_error, 3),
        "rotation_degrees": round(rotation_degrees, 2),
        "outer_corners_px": [[round(float(x), 2), round(float(y), 2)] for x, y in outer],
    }
    return calibration, detection


def write_board_detection_debug(image, stem: str, corners: np.ndarray, calibration: dict, detection: dict) -> str:
    BOARD_DETECTION_DIR.mkdir(parents=True, exist_ok=True)
    debug = image.copy()

    for square in board_squares(calibration):
        rect = square.get("rect_px", {})
        x = int(round(float(rect.get("x", 0))))
        y = int(round(float(rect.get("y", 0))))
        width = int(round(float(rect.get("width", 0))))
        height = int(round(float(rect.get("height", 0))))
        cv2.rectangle(debug, (x, y), (x + width, y + height), (64, 180, 255), 1)

    outer = np.array(detection.get("outer_corners_px", []), dtype=np.int32)
    if outer.shape == (4, 2):
        cv2.polylines(debug, [outer], True, (80, 240, 120), 4, cv2.LINE_AA)

    for x, y in corners:
        cv2.circle(debug, (int(round(float(x))), int(round(float(y)))), 4, (255, 80, 80), -1, cv2.LINE_AA)

    label = (
        f"Board detection error {detection.get('reprojection_error_px')} px, "
        f"rotation {detection.get('rotation_degrees')} deg"
    )
    cv2.rectangle(debug, (16, 16), (min(debug.shape[1] - 16, 760), 54), (0, 0, 0), -1)
    cv2.putText(debug, label, (24, 42), cv2.FONT_HERSHEY_SIMPLEX, 0.68, (245, 245, 245), 2, cv2.LINE_AA)

    output_path = BOARD_DETECTION_DIR / f"{stem}.jpg"
    ok = cv2.imwrite(str(output_path), debug)
    if not ok:
        raise RuntimeError("Failed to write board detection debug image.")
    return f"/captures/board-detections/{stem}.jpg"


def detect_board_from_capture(filename: str) -> dict:
    image_path = root_capture_image_path(filename)
    image = cv2.imread(str(image_path))
    if image is None:
        raise ValueError("OpenCV could not read the capture image.")

    height, width = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    corners, detector = detect_chessboard_corners(gray)
    metadata_path = image_path.with_suffix(".json")
    metadata = capture_metadata(metadata_path) or {}
    existing_calibration = normalize_calibration(metadata.get("calibration") or load_calibration())
    calibration, detection = detected_calibration_from_corners(
        corners,
        width,
        height,
        existing_calibration["white_bottom"],
    )
    detection["detector"] = detector
    detection["debug_url"] = write_board_detection_debug(image, image_path.stem, corners, calibration, detection)

    pieces = metadata.get("pieces") or {}
    metadata.update(
        {
            "image": metadata.get("image") or image_path.name,
            "resolution": [width, height],
            "calibration": calibration,
            "squares": board_squares(calibration),
            "pieces": pieces,
            "fen": metadata.get("fen") or position_to_fen(pieces),
            "board_detection": detection,
        }
    )
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
    save_calibration(calibration)
    manifest = export_square_crops(filename)
    return {
        "calibration": calibration,
        "detection": detection,
        "metadata": str(metadata_path),
        "manifest": manifest,
    }


def dataset_manifest() -> dict:
    rows = []
    captures = 0
    analyzed_captures = 0
    piece_counts = {"empty": 0}

    if SQUARE_CROPS_DIR.exists():
        for manifest_path in sorted(SQUARE_CROPS_DIR.glob("*/manifest.json")):
            manifest = capture_metadata(manifest_path) or {}
            captures += 1
            if manifest.get("analysis_created_at"):
                analyzed_captures += 1

            for crop in manifest.get("crops", []):
                analysis = crop.get("analysis")
                if not analysis:
                    continue

                piece = crop.get("piece") or ""
                label = piece or "empty"
                piece_counts[label] = piece_counts.get(label, 0) + 1
                rows.append(
                    {
                        "source": manifest.get("source"),
                        "square": crop.get("square"),
                        "piece": piece,
                        "occupied": bool(piece),
                        "url": crop.get("url"),
                        "fen": manifest.get("fen"),
                        "features": {
                            "mean_brightness": analysis.get("mean_brightness"),
                            "contrast": analysis.get("contrast"),
                            "edge_density": analysis.get("edge_density"),
                        },
                    }
                )

    return {
        "ok": True,
        "summary": {
            "captures_with_crops": captures,
            "analyzed_captures": analyzed_captures,
            "rows": len(rows),
            "occupied_rows": sum(1 for row in rows if row["occupied"]),
            "empty_rows": sum(1 for row in rows if not row["occupied"]),
            "piece_counts": dict(sorted(piece_counts.items())),
        },
        "rows": rows,
    }


def dataset_csv_text() -> str:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=DATASET_CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()

    for row in dataset_manifest()["rows"]:
        features = row.get("features") or {}
        writer.writerow(
            {
                "source": row.get("source") or "",
                "square": row.get("square") or "",
                "piece": row.get("piece") or "",
                "occupied": "1" if row.get("occupied") else "0",
                "url": row.get("url") or "",
                "fen": row.get("fen") or "",
                "mean_brightness": features.get("mean_brightness") or "",
                "contrast": features.get("contrast") or "",
                "edge_density": features.get("edge_density") or "",
            }
        )

    return output.getvalue()


def feature_vector(features: dict) -> list[float] | None:
    values = []
    for name in OCCUPANCY_FEATURES:
        value = features.get(name)
        if value is None:
            return None
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            return None
    return values


def mean_vector(vectors: list[list[float]]) -> list[float]:
    return [sum(vector[index] for vector in vectors) / len(vectors) for index in range(len(OCCUPANCY_FEATURES))]


def normalize_vector(vector: list[float], means: list[float], scales: list[float]) -> list[float]:
    return [(value - means[index]) / scales[index] for index, value in enumerate(vector)]


def train_occupancy_model() -> dict:
    rows = dataset_manifest()["rows"]
    training_rows = []
    for row in rows:
        vector = feature_vector(row.get("features", {}))
        if vector is None:
            continue
        training_rows.append({"occupied": bool(row.get("occupied")), "vector": vector})

    occupied = [row["vector"] for row in training_rows if row["occupied"]]
    empty = [row["vector"] for row in training_rows if not row["occupied"]]
    if not occupied or not empty:
        raise ValueError("Need at least one occupied and one empty analyzed square crop to train.")

    all_vectors = [row["vector"] for row in training_rows]
    means = mean_vector(all_vectors)
    scales = []
    for index in range(len(OCCUPANCY_FEATURES)):
        variance = sum((vector[index] - means[index]) ** 2 for vector in all_vectors) / len(all_vectors)
        scales.append(max(variance ** 0.5, 1e-6))

    normalized_occupied = [normalize_vector(vector, means, scales) for vector in occupied]
    normalized_empty = [normalize_vector(vector, means, scales) for vector in empty]
    model = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "feature_names": list(OCCUPANCY_FEATURES),
        "normalization": {
            "mean": dict(zip(OCCUPANCY_FEATURES, [round(value, 6) for value in means])),
            "scale": dict(zip(OCCUPANCY_FEATURES, [round(value, 6) for value in scales])),
        },
        "centroids": {
            "occupied": dict(zip(OCCUPANCY_FEATURES, [round(value, 6) for value in mean_vector(normalized_occupied)])),
            "empty": dict(zip(OCCUPANCY_FEATURES, [round(value, 6) for value in mean_vector(normalized_empty)])),
        },
        "training": {
            "rows": len(training_rows),
            "occupied_rows": len(occupied),
            "empty_rows": len(empty),
        },
    }
    OCCUPANCY_MODEL_PATH.write_text(json.dumps(model, indent=2) + "\n")
    return model


def load_occupancy_model() -> dict | None:
    if not OCCUPANCY_MODEL_PATH.exists():
        return None
    try:
        return json.loads(OCCUPANCY_MODEL_PATH.read_text())
    except (OSError, ValueError, TypeError):
        return None


def occupancy_model_payload() -> dict:
    model = load_occupancy_model()
    return {"ok": True, "available": model is not None, "model": model}


def squared_distance(left: list[float], right: list[float]) -> float:
    return sum((left[index] - right[index]) ** 2 for index in range(len(left)))


def predict_occupancy(features: dict, model: dict) -> dict:
    vector = feature_vector(features)
    if vector is None:
        raise ValueError("Square crop is missing analysis features.")

    means = [float(model["normalization"]["mean"][name]) for name in OCCUPANCY_FEATURES]
    scales = [float(model["normalization"]["scale"][name]) for name in OCCUPANCY_FEATURES]
    normalized = normalize_vector(vector, means, scales)
    occupied_centroid = [float(model["centroids"]["occupied"][name]) for name in OCCUPANCY_FEATURES]
    empty_centroid = [float(model["centroids"]["empty"][name]) for name in OCCUPANCY_FEATURES]
    occupied_distance = squared_distance(normalized, occupied_centroid)
    empty_distance = squared_distance(normalized, empty_centroid)
    predicted_occupied = occupied_distance <= empty_distance
    confidence = abs(empty_distance - occupied_distance) / max(empty_distance + occupied_distance, 1e-6)
    return {
        "occupied": predicted_occupied,
        "confidence": round(confidence, 4),
        "distance_to_occupied": round(occupied_distance, 4),
        "distance_to_empty": round(empty_distance, 4),
    }


def predict_capture_occupancy(filename: str) -> dict:
    model = load_occupancy_model()
    if model is None:
        raise ValueError("Train the occupancy model before prediction.")

    manifest = analyze_square_crops(filename)
    predictions = []
    for crop in manifest.get("crops", []):
        analysis = crop.get("analysis")
        if not analysis:
            continue
        prediction = predict_occupancy(analysis, model)
        crop["occupancy_prediction"] = prediction
        predictions.append(
            {
                "square": crop.get("square"),
                "piece": crop.get("piece"),
                **prediction,
            }
        )

    manifest["crops"] = manifest.get("crops", [])
    manifest["occupancy_predictions_at"] = datetime.now().isoformat(timespec="seconds")
    manifest["occupancy_model_created_at"] = model.get("created_at")
    manifest_path = square_crop_manifest_path(filename)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    manifest["manifest_url"] = f"/captures/square-crops/{Path(filename).stem}/manifest.json"
    manifest["predictions"] = predictions
    return manifest


def piece_image_vector(path: Path) -> list[float]:
    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise ValueError(f"OpenCV could not read crop image {path.name}.")

    resized = cv2.resize(image, (PIECE_IMAGE_SIZE, PIECE_IMAGE_SIZE), interpolation=cv2.INTER_AREA)
    gray = resized.astype(np.float32) / 255.0
    mean = float(np.mean(gray))
    std = max(float(np.std(gray)), 1e-6)
    normalized_gray = (gray - mean) / std
    edges = cv2.Canny(resized, 50, 150).astype(np.float32) / 255.0
    vector = np.concatenate([normalized_gray.flatten(), edges.flatten()])
    return [float(value) for value in vector]


def average_piece_vectors(vectors: list[list[float]]) -> list[float]:
    length = len(vectors[0])
    return [sum(vector[index] for vector in vectors) / len(vectors) for index in range(length)]


def labeled_piece_vectors() -> list[dict]:
    samples = []
    if not SQUARE_CROPS_DIR.exists():
        return samples

    for manifest_path in sorted(SQUARE_CROPS_DIR.glob("*/manifest.json")):
        manifest = capture_metadata(manifest_path) or {}
        crop_root = manifest_path.parent
        source = manifest.get("source")
        for crop in manifest.get("crops", []):
            label = crop.get("piece") or "empty"
            if label != "empty" and label not in PIECE_CODES:
                continue
            crop_path = crop_root / crop.get("filename", "")
            if not crop_path.exists():
                continue
            samples.append(
                {
                    "label": label,
                    "square": crop.get("square"),
                    "source": source,
                    "vector": piece_image_vector(crop_path),
                }
            )

    return samples


def train_piece_model() -> dict:
    samples = labeled_piece_vectors()
    by_label: dict[str, list[list[float]]] = {}
    for sample in samples:
        by_label.setdefault(sample["label"], []).append(sample["vector"])

    non_empty_labels = [label for label in by_label if label != "empty"]
    if "empty" not in by_label or not non_empty_labels:
        raise ValueError("Need at least one empty crop and one labeled piece crop to train the piece model.")

    centroids = {}
    for label, vectors in sorted(by_label.items()):
        centroids[label] = [round(value, 6) for value in average_piece_vectors(vectors)]

    label_counts = {label: len(vectors) for label, vectors in sorted(by_label.items())}
    model = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "image_size": PIECE_IMAGE_SIZE,
        "vector_length": PIECE_IMAGE_SIZE * PIECE_IMAGE_SIZE * 2,
        "labels": sorted(by_label.keys()),
        "centroids": centroids,
        "training": {
            "rows": len(samples),
            "labels": label_counts,
            "piece_labels": sorted(non_empty_labels),
            "piece_rows": sum(count for label, count in label_counts.items() if label != "empty"),
            "empty_rows": label_counts.get("empty", 0),
        },
    }
    PIECE_MODEL_PATH.write_text(json.dumps(model, indent=2) + "\n")
    return model


def piece_centroid_model(samples: list[dict]) -> dict:
    by_label: dict[str, list[list[float]]] = {}
    for sample in samples:
        by_label.setdefault(sample["label"], []).append(sample["vector"])
    return {
        "centroids": {
            label: average_piece_vectors(vectors)
            for label, vectors in sorted(by_label.items())
            if vectors
        }
    }


def score_piece_samples(samples: list[dict], model: dict) -> dict:
    correct = 0
    rows = []
    confusion: dict[str, dict[str, int]] = {}
    low_confidence = []
    errors = []

    for sample in samples:
        prediction = predict_piece_from_vector(sample["vector"], model)
        expected = sample["label"]
        predicted = prediction["label"]
        is_correct = expected == predicted
        correct += 1 if is_correct else 0
        confusion.setdefault(expected, {})
        confusion[expected][predicted] = confusion[expected].get(predicted, 0) + 1
        row = {
            "source": sample.get("source"),
            "square": sample.get("square"),
            "expected": expected,
            "predicted": predicted,
            "confidence": prediction.get("confidence"),
            "correct": is_correct,
        }
        rows.append(row)
        if prediction.get("confidence", 1.0) < 0.35:
            low_confidence.append(row)
        if not is_correct:
            errors.append(row)

    total = len(samples)
    return {
        "rows": total,
        "correct": correct,
        "accuracy": round(correct / total, 4) if total else None,
        "confusion": confusion,
        "errors": errors[:20],
        "low_confidence": low_confidence[:20],
    }


def evaluate_piece_model() -> dict:
    samples = labeled_piece_vectors()
    label_counts: dict[str, int] = {}
    for sample in samples:
        label_counts[sample["label"]] = label_counts.get(sample["label"], 0) + 1
    if len(label_counts) < 2:
        raise ValueError("Need at least two labels in square-crop data to evaluate the piece model.")

    full_model = piece_centroid_model(samples)
    training_score = score_piece_samples(samples, full_model)

    loo_samples = []
    skipped = []
    for index, sample in enumerate(samples):
        training_samples = [other for other_index, other in enumerate(samples) if other_index != index]
        labels_after_holdout = {other["label"] for other in training_samples}
        if sample["label"] not in labels_after_holdout or len(labels_after_holdout) < 2:
            skipped.append(
                {
                    "source": sample.get("source"),
                    "square": sample.get("square"),
                    "label": sample.get("label"),
                }
            )
            continue
        model = piece_centroid_model(training_samples)
        loo_samples.append({**sample, "model": model})

    loo_rows = []
    for sample in loo_samples:
        model = sample.pop("model")
        prediction = predict_piece_from_vector(sample["vector"], model)
        loo_rows.append({**sample, "prediction": prediction})

    loo_correct = 0
    loo_confusion: dict[str, dict[str, int]] = {}
    loo_errors = []
    loo_low_confidence = []
    for row in loo_rows:
        expected = row["label"]
        prediction = row["prediction"]
        predicted = prediction["label"]
        is_correct = expected == predicted
        loo_correct += 1 if is_correct else 0
        loo_confusion.setdefault(expected, {})
        loo_confusion[expected][predicted] = loo_confusion[expected].get(predicted, 0) + 1
        result = {
            "source": row.get("source"),
            "square": row.get("square"),
            "expected": expected,
            "predicted": predicted,
            "confidence": prediction.get("confidence"),
            "correct": is_correct,
        }
        if prediction.get("confidence", 1.0) < 0.35:
            loo_low_confidence.append(result)
        if not is_correct:
            loo_errors.append(result)

    loo_total = len(loo_rows)
    leave_one_out = {
        "rows": loo_total,
        "skipped_rows": len(skipped),
        "accuracy": round(loo_correct / loo_total, 4) if loo_total else None,
        "correct": loo_correct,
        "confusion": loo_confusion,
        "errors": loo_errors[:20],
        "low_confidence": loo_low_confidence[:20],
        "skipped": skipped[:20],
    }
    return {
        "ok": True,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "summary": {
            "rows": len(samples),
            "labels": dict(sorted(label_counts.items())),
            "training_accuracy": training_score["accuracy"],
            "leave_one_out_accuracy": leave_one_out["accuracy"],
            "leave_one_out_rows": leave_one_out["rows"],
            "leave_one_out_skipped_rows": leave_one_out["skipped_rows"],
        },
        "training": training_score,
        "leave_one_out": leave_one_out,
    }


def load_piece_model() -> dict | None:
    if not PIECE_MODEL_PATH.exists():
        return None
    try:
        return json.loads(PIECE_MODEL_PATH.read_text())
    except (OSError, ValueError, TypeError):
        return None


def piece_model_payload() -> dict:
    model = load_piece_model()
    return {"ok": True, "available": model is not None, "model": model}


def predict_piece_from_vector(vector: list[float], model: dict) -> dict:
    distances = []
    for label, centroid in model.get("centroids", {}).items():
        centroid_vector = [float(value) for value in centroid]
        if len(centroid_vector) != len(vector):
            continue
        distance = squared_distance(vector, centroid_vector) / max(len(vector), 1)
        distances.append((distance, label))

    if not distances:
        raise ValueError("Piece model has no compatible label centroids.")

    distances.sort(key=lambda item: item[0])
    best_distance, best_label = distances[0]
    second_distance = distances[1][0] if len(distances) > 1 else best_distance
    confidence = (second_distance - best_distance) / max(second_distance, 1e-6) if second_distance else 1.0
    return {
        "label": best_label,
        "piece": "" if best_label == "empty" else best_label,
        "occupied": best_label != "empty",
        "confidence": round(max(0.0, min(1.0, confidence)), 4),
        "distance": round(best_distance, 5),
        "runner_up": distances[1][1] if len(distances) > 1 else None,
    }


def predict_capture_pieces(filename: str) -> dict:
    model = load_piece_model()
    if model is None:
        raise ValueError("Train the piece model before prediction.")

    manifest_path = square_crop_manifest_path(filename)
    if not manifest_path.exists():
        manifest = export_square_crops(filename)
    else:
        manifest = capture_metadata(manifest_path) or {}

    crop_root = manifest_path.parent
    predictions = []
    predicted_pieces = {}
    valid_squares = set(square_names())
    for crop in manifest.get("crops", []):
        crop_path = crop_root / crop.get("filename", "")
        prediction = predict_piece_from_vector(piece_image_vector(crop_path), model)
        crop["piece_prediction"] = prediction
        square = crop.get("square")
        if prediction["piece"] and square in valid_squares:
            predicted_pieces[square] = prediction["piece"]
        predictions.append(
            {
                "square": square,
                "manual_piece": crop.get("piece"),
                **prediction,
            }
        )

    predicted_pieces = normalize_position(predicted_pieces)
    manifest["crops"] = manifest.get("crops", [])
    manifest["piece_predictions_at"] = datetime.now().isoformat(timespec="seconds")
    manifest["piece_model_created_at"] = model.get("created_at")
    manifest["predicted_pieces"] = predicted_pieces
    manifest["predicted_fen"] = position_to_fen(predicted_pieces)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    manifest["manifest_url"] = f"/captures/square-crops/{Path(filename).stem}/manifest.json"
    manifest["predictions"] = predictions
    return manifest


def summarize_piece_recognition(predictions: list[dict]) -> dict:
    confidences = []
    low_confidence = []
    predicted_piece_count = 0
    for prediction in predictions:
        if prediction.get("piece"):
            predicted_piece_count += 1
        confidence = prediction.get("confidence")
        if confidence is None:
            continue
        confidence = float(confidence)
        confidences.append(confidence)
        if confidence < 0.35:
            low_confidence.append(
                {
                    "square": prediction.get("square"),
                    "label": prediction.get("label"),
                    "confidence": round(confidence, 4),
                }
            )

    return {
        "predicted_piece_count": predicted_piece_count,
        "squares": len(predictions),
        "average_confidence": round(sum(confidences) / len(confidences), 4) if confidences else None,
        "low_confidence_count": len(low_confidence),
        "low_confidence": low_confidence[:12],
    }


def recognize_capture_position(filename: str) -> dict:
    export_square_crops(filename)
    manifest = predict_capture_pieces(filename)
    predictions = manifest.get("predictions", [])
    summary = summarize_piece_recognition(predictions)
    manifest["position_recognized_at"] = datetime.now().isoformat(timespec="seconds")
    manifest["recognition_summary"] = summary
    manifest_path = square_crop_manifest_path(filename)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def recognition_payload(manifest: dict) -> dict:
    return {
        "manifest": manifest,
        "predicted_pieces": manifest.get("predicted_pieces", {}),
        "predicted_fen": manifest.get("predicted_fen"),
        "recognition_summary": manifest.get("recognition_summary", {}),
    }


def readiness_payload() -> dict:
    health = camera_manager.status()
    config_lines = camera_config_lines()
    config_recommendation = camera_config_recommendation(config_lines)
    captures = capture_entries()
    dataset = dataset_manifest()
    dataset_summary = dataset.get("summary", {})
    occupancy_model = load_occupancy_model()
    piece_model = load_piece_model()
    calibration_ready = CALIBRATION_PATH.exists() or any(capture.get("board_detected_at") for capture in captures)
    has_captures = bool(captures)
    has_analyzed_rows = dataset_summary.get("rows", 0) > 0
    has_piece_labels = len([label for label in dataset_summary.get("piece_counts", {}) if label != "empty"]) > 0
    piece_model_ready = piece_model is not None
    boot_config_ready = bool(health["camera_available"] or config_recommendation.get("level") == "ok")
    recognition_ready = bool(health["camera_available"] and calibration_ready and piece_model_ready)

    gates = [
        {
            "id": "camera",
            "label": "Camera",
            "ready": bool(health["camera_available"]),
            "detail": "Connected" if health["camera_available"] else "Waiting for CSI camera",
            "action": None if health["camera_available"] else "Connect camera, reboot, then refresh",
        },
        {
            "id": "boot_config",
            "label": "Boot config",
            "ready": boot_config_ready,
            "detail": config_recommendation.get("message") or "Camera boot config checked",
            "action": None if boot_config_ready else "If detection fails, run ./set_camera_autodetect.sh and reboot",
        },
        {
            "id": "captures",
            "label": "Snapshots",
            "ready": has_captures,
            "detail": f"{len(captures)} saved",
            "action": None if has_captures else "Save a board snapshot or use Save + detect board",
        },
        {
            "id": "calibration",
            "label": "Board map",
            "ready": calibration_ready,
            "detail": "Saved" if calibration_ready else "Default only",
            "action": None if calibration_ready else "Use Save + detect board on an empty board",
        },
        {
            "id": "dataset",
            "label": "Training rows",
            "ready": has_analyzed_rows,
            "detail": f"{dataset_summary.get('rows', 0)} rows",
            "action": None if has_analyzed_rows else "Export and analyze square crops",
        },
        {
            "id": "piece_labels",
            "label": "Piece labels",
            "ready": has_piece_labels,
            "detail": f"{dataset_summary.get('occupied_rows', 0)} labeled pieces",
            "action": None if has_piece_labels else "Paint pieces before exporting crops",
        },
        {
            "id": "piece_model",
            "label": "Piece model",
            "ready": piece_model_ready,
            "detail": (
                f"{piece_model.get('training', {}).get('rows', 0)} rows"
                if piece_model_ready
                else "Not trained"
            ),
            "action": None if piece_model_ready else "Train pieces",
        },
        {
            "id": "recognition",
            "label": "Recognition",
            "ready": recognition_ready,
            "detail": "Ready" if recognition_ready else "Not ready",
            "action": None if recognition_ready else "Finish waiting steps",
        },
    ]
    waiting = [gate for gate in gates if not gate["ready"]]
    return {
        "ok": True,
        "ready": not waiting,
        "next_action": waiting[0]["action"] if waiting else "Save + recognize",
        "gates": gates,
        "summary": {
            "captures": len(captures),
            "dataset_rows": dataset_summary.get("rows", 0),
            "occupied_rows": dataset_summary.get("occupied_rows", 0),
            "piece_model_available": piece_model_ready,
            "occupancy_model_available": occupancy_model is not None,
            "camera_available": bool(health["camera_available"]),
            "camera_config_level": config_recommendation.get("level"),
            "camera_config_action": config_recommendation.get("recommended_action"),
        },
    }


def save_frame_capture(frame, extra_metadata: dict | None = None, filename_prefix: str = "snapshot") -> dict:
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{filename_prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    image_path = CAPTURE_DIR / f"{stem}.jpg"
    metadata_path = CAPTURE_DIR / f"{stem}.json"
    height, width = frame.shape[:2]
    calibration = normalize_calibration(load_calibration(), width / height)
    squares = board_squares(calibration, (width, height))
    position = load_position()
    image_path.write_bytes(encode_jpeg(frame))
    metadata = {
        "image": image_path.name,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "capture_mode": "single",
        "resolution": [width, height],
        "camera_settings": active_camera_settings(),
        "calibration": calibration,
        "squares": squares,
        "pieces": position,
        "fen": position_to_fen(position),
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    metadata_path.write_text(
        json.dumps(metadata, indent=2, default=str)
        + "\n"
    )
    return {
        "filename": image_path.name,
        "path": str(image_path),
        "metadata": str(metadata_path),
        "calibration": calibration,
        "squares": squares,
        "pieces": position,
        "fen": position_to_fen(position),
    }


def board_squares(calibration: dict | None = None, resolution: tuple[int, int] | None = None) -> list[dict]:
    frame_width, frame_height = resolution or active_frame_size()
    image_aspect = frame_width / frame_height
    calibration = normalize_calibration(calibration or load_calibration(), image_aspect)
    square_size_pct_x = calibration["size"] / 8.0
    square_size_px = frame_width * (square_size_pct_x / 100.0)
    top_px = frame_height * (calibration["top"] / 100.0)
    left_px = frame_width * (calibration["left"] / 100.0)
    squares = []

    for row in range(8):
        for col in range(8):
            if calibration["white_bottom"]:
                file_name = FILES[col]
                rank = 8 - row
            else:
                file_name = FILES[7 - col]
                rank = row + 1

            x = left_px + (col * square_size_px)
            y = top_px + (row * square_size_px)
            squares.append(
                {
                    "square": f"{file_name}{rank}",
                    "row": row,
                    "col": col,
                    "rect_pct": {
                        "left": round(calibration["left"] + (col * square_size_pct_x), 4),
                        "top": round(
                            calibration["top"]
                            + ((row * square_size_px / frame_height) * 100.0),
                            4,
                        ),
                        "width": round(square_size_pct_x, 4),
                        "height": round((square_size_px / frame_height) * 100.0, 4),
                    },
                    "rect_px": {
                        "x": round(x, 2),
                        "y": round(y, 2),
                        "width": round(square_size_px, 2),
                        "height": round(square_size_px, 2),
                    },
                }
            )

    return squares


def frame_to_display_rgb(frame, settings: dict | None = None):
    settings = settings or active_camera_settings()
    if settings.get("color_order") == "bgr":
        return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    return frame.copy()


def display_rgb_to_frame_order(display_rgb, settings: dict | None = None):
    settings = settings or active_camera_settings()
    if settings.get("color_order") == "bgr":
        return cv2.cvtColor(display_rgb, cv2.COLOR_RGB2BGR)
    return display_rgb.copy()


def apply_display_tuning(display_rgb, settings: dict | None = None):
    settings = settings or active_camera_settings()
    shadow_lift = float(settings.get("shadow_lift", 0.0) or 0.0)
    purple_fix = float(settings.get("purple_fix", 0.0) or 0.0)
    if shadow_lift <= 0 and purple_fix <= 0:
        return display_rgb

    image = display_rgb.astype(np.float32) / 255.0
    luminance = (
        (0.2126 * image[:, :, 0])
        + (0.7152 * image[:, :, 1])
        + (0.0722 * image[:, :, 2])
    )
    shadow_weight = np.clip((0.62 - luminance) / 0.62, 0.0, 1.0) ** 1.35

    if shadow_lift > 0:
        lift = shadow_weight[:, :, None] * min(shadow_lift, 1.0) * 0.58
        image = image + ((1.0 - image) * lift)
        luminance = (
            (0.2126 * image[:, :, 0])
            + (0.7152 * image[:, :, 1])
            + (0.0722 * image[:, :, 2])
        )

    if purple_fix > 0:
        shadow_weight = np.clip((0.58 - luminance) / 0.58, 0.0, 1.0) ** 1.2
        amount = shadow_weight[:, :, None] * min(purple_fix, 1.0)
        neutral = luminance[:, :, None]
        image = (image * (1.0 - (amount * 0.62))) + (neutral * amount * 0.62)
        magenta = np.clip(
            (((image[:, :, 0] + image[:, :, 2]) * 0.5) - image[:, :, 1]) / 0.35,
            0.0,
            1.0,
        )
        magenta *= shadow_weight * min(purple_fix, 1.0)
        channel_pull = magenta * 0.45
        image[:, :, 0] = (image[:, :, 0] * (1.0 - channel_pull)) + (image[:, :, 1] * channel_pull)
        image[:, :, 2] = (image[:, :, 2] * (1.0 - channel_pull)) + (image[:, :, 1] * channel_pull)

    return np.clip(image * 255.0, 0, 255).astype(np.uint8)


def merge_hdr_display_frames(display_frames: list) -> np.ndarray:
    if not display_frames:
        raise ValueError("HDR merge needs at least one frame.")
    if len(display_frames) == 1:
        return display_frames[0]

    normalized = [frame.astype(np.float32) / 255.0 for frame in display_frames]
    short = normalized[0]
    middle = normalized[len(normalized) // 2]
    long = normalized[-1]
    luminance = (
        (0.2126 * middle[:, :, 0])
        + (0.7152 * middle[:, :, 1])
        + (0.0722 * middle[:, :, 2])
    )
    shadow_weight = np.clip((0.52 - luminance) / 0.52, 0.0, 1.0) ** 1.35
    highlight_weight = np.clip((luminance - 0.72) / 0.28, 0.0, 1.0) ** 1.35
    middle_weight = np.ones_like(luminance)

    merged = (
        (short * highlight_weight[:, :, None])
        + (middle * middle_weight[:, :, None])
        + (long * shadow_weight[:, :, None])
    ) / (highlight_weight + middle_weight + shadow_weight)[:, :, None]
    return np.clip(merged * 255.0, 0, 255).astype(np.uint8)


def encode_jpeg(rgb_frame) -> bytes:
    settings = active_camera_settings()
    display_rgb = apply_display_tuning(frame_to_display_rgb(rgb_frame, settings), settings)
    bgr_frame = cv2.cvtColor(display_rgb, cv2.COLOR_RGB2BGR)
    quality = settings["jpeg_quality"]
    ok, encoded = cv2.imencode(
        ".jpg",
        bgr_frame,
        [int(cv2.IMWRITE_JPEG_QUALITY), quality],
    )
    if not ok:
        raise RuntimeError("OpenCV failed to encode JPEG frame.")
    return encoded.tobytes()


def placeholder_jpeg() -> bytes:
    frame_width, frame_height = active_frame_size()
    canvas = np.full((frame_height, frame_width, 3), (32, 36, 42), dtype=np.uint8)
    accent = (58, 168, 123)
    muted = (178, 187, 197)
    cv2.rectangle(canvas, (0, 0), (frame_width - 1, frame_height - 1), accent, 4)
    cv2.putText(
        canvas,
        "Camera not detected",
        (64, frame_height // 2 - 18),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.35,
        (240, 243, 246),
        3,
        cv2.LINE_AA,
    )
    cv2.putText(
        canvas,
        "Connect the CSI module to CAM/DISP0, reboot, then refresh this page.",
        (64, frame_height // 2 + 42),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.75,
        muted,
        2,
        cv2.LINE_AA,
    )
    ok, encoded = cv2.imencode(
        ".jpg",
        canvas,
        [int(cv2.IMWRITE_JPEG_QUALITY), active_camera_settings()["jpeg_quality"]],
    )
    if not ok:
        raise RuntimeError("OpenCV failed to encode placeholder frame.")
    return encoded.tobytes()


def mjpeg_frames():
    while True:
        frame = camera_manager.capture_rgb()
        if frame is None:
            jpeg = placeholder_jpeg()
        else:
            jpeg = encode_jpeg(frame)

        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
        )
        delay = 1.0 / max(float(active_camera_settings()["stream_fps"]), 0.1)
        time.sleep(delay)


INDEX_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ChessV2 Camera</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #101317;
      --panel: #181d23;
      --panel-soft: #202832;
      --line: #35404c;
      --text: #f3f6f8;
      --muted: #aeb8c3;
      --accent: #3aa87b;
      --accent-strong: #51c293;
      --warn: #d9a441;
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }

    main {
      width: min(1180px, calc(100vw - 32px));
      margin: 0 auto;
      padding: 24px 0;
    }

    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 16px;
    }

    h1 {
      margin: 0;
      font-size: clamp(1.25rem, 2vw, 1.8rem);
      font-weight: 700;
      letter-spacing: 0;
    }

    .status {
      display: flex;
      align-items: center;
      gap: 10px;
      color: var(--muted);
      font-size: 0.95rem;
      white-space: nowrap;
    }

    .dot {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: var(--warn);
      box-shadow: 0 0 0 4px rgba(217, 164, 65, 0.16);
    }

    .dot.ready {
      background: var(--accent-strong);
      box-shadow: 0 0 0 4px rgba(81, 194, 147, 0.16);
    }

    .viewer {
      position: relative;
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #080a0d;
      aspect-ratio: 16 / 9;
    }

    .viewer img {
      display: block;
      width: 100%;
      height: 100%;
      object-fit: contain;
      background: #080a0d;
    }

    .board-overlay {
      position: absolute;
      left: calc(var(--board-left, 26.5) * 1%);
      top: calc(var(--board-top, 8) * 1%);
      width: calc(var(--board-size, 47.25) * 1%);
      aspect-ratio: 1 / 1;
      display: grid;
      grid-template-columns: repeat(8, minmax(0, 1fr));
      grid-template-rows: repeat(8, minmax(0, 1fr));
      pointer-events: auto;
      border: 2px solid rgba(81, 194, 147, 0.95);
      background-image:
        linear-gradient(to right, rgba(81, 194, 147, 0.72) 1px, transparent 1px),
        linear-gradient(to bottom, rgba(81, 194, 147, 0.72) 1px, transparent 1px);
      background-size: 12.5% 12.5%;
      box-shadow: 0 0 0 999px rgba(0, 0, 0, 0.16);
    }

    .square-label {
      display: flex;
      flex-direction: column;
      align-items: flex-start;
      justify-content: flex-start;
      gap: 2px;
      padding: 3px;
      color: rgba(243, 246, 248, 0.92);
      font-size: 0.68rem;
      font-weight: 800;
      line-height: 1;
      text-shadow: 0 1px 2px rgba(0, 0, 0, 0.8);
      overflow: hidden;
      cursor: crosshair;
      pointer-events: auto;
    }

    .square-label.has-piece {
      background: rgba(81, 194, 147, 0.22);
    }

    .piece-code {
      align-self: center;
      margin-top: auto;
      margin-bottom: auto;
      padding: 2px 4px;
      border-radius: 4px;
      background: rgba(8, 10, 13, 0.72);
      color: #f3f6f8;
      font-size: 0.82rem;
    }

    .square-name {
      opacity: 0.9;
    }

    .board-overlay.hidden {
      display: none;
    }

    .toolbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-top: 14px;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
    }

    button,
    .button-link {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 42px;
      border: 0;
      border-radius: 7px;
      padding: 0 16px;
      background: var(--accent);
      color: #05120d;
      font-weight: 700;
      font-size: 0.95rem;
      cursor: pointer;
      text-decoration: none;
    }

    button.secondary,
    .button-link.secondary {
      border: 1px solid var(--line);
      background: transparent;
      color: var(--text);
    }

    .toggle {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      min-height: 42px;
      color: var(--text);
      font-weight: 650;
      white-space: nowrap;
    }

    .toggle input {
      width: 18px;
      height: 18px;
      accent-color: var(--accent-strong);
    }

    .actions {
      display: flex;
      align-items: center;
      gap: 14px;
      flex-wrap: wrap;
    }

    button:disabled {
      cursor: progress;
      opacity: 0.72;
    }

    .message {
      color: var(--muted);
      font-size: 0.95rem;
      overflow-wrap: anywhere;
      text-align: right;
    }

    .last-capture-panel {
      margin-top: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      overflow: hidden;
    }

    .last-capture-panel[hidden] {
      display: none;
    }

    .last-capture-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 12px 14px;
      color: var(--muted);
      font-size: 0.9rem;
    }

    .last-capture-header strong {
      color: var(--text);
      font-size: 0.78rem;
      text-transform: uppercase;
      letter-spacing: 0;
    }

    .last-capture-panel img {
      display: block;
      width: 100%;
      max-height: 72vh;
      object-fit: contain;
      background: #080a0d;
      border-top: 1px solid var(--line);
    }

    .details {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
      gap: 10px;
      margin-top: 10px;
      color: var(--muted);
      font-size: 0.9rem;
    }

    .detail {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-soft);
      padding: 12px;
      min-width: 0;
      overflow-wrap: anywhere;
    }

    .detail strong {
      display: block;
      margin-bottom: 4px;
      color: var(--text);
      font-size: 0.78rem;
      text-transform: uppercase;
      letter-spacing: 0;
    }

    .readiness-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-top: 10px;
    }

    .readiness-header h2 {
      margin: 0;
      font-size: 1rem;
      letter-spacing: 0;
    }

    .readiness-list {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 10px;
      margin-top: 10px;
    }

    .readiness-item {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-soft);
      padding: 12px;
      color: var(--muted);
      font-size: 0.84rem;
      min-width: 0;
      overflow-wrap: anywhere;
    }

    .readiness-item.ready {
      border-color: rgba(81, 194, 147, 0.72);
    }

    .readiness-item.waiting {
      border-color: rgba(217, 164, 65, 0.76);
    }

    .readiness-item strong {
      display: block;
      margin-bottom: 5px;
      color: var(--text);
      font-size: 0.78rem;
      text-transform: uppercase;
      letter-spacing: 0;
    }

    .readiness-action {
      margin-top: 6px;
      color: var(--text);
      font-weight: 650;
    }

    .diagnostics {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      gap: 10px;
      margin-top: 10px;
    }

    .diagnostic-output {
      min-height: 116px;
      margin: 0;
      overflow: auto;
      white-space: pre-wrap;
      color: var(--muted);
      font: 0.82rem ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      line-height: 1.45;
    }

    .calibration {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr)) auto auto;
      gap: 12px;
      align-items: end;
      margin-top: 10px;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
    }

    .camera-controls {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 12px;
      align-items: end;
      margin-top: 10px;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
    }

    .tuning-controls {
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    }

    .tool-panel {
      margin-top: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      overflow: hidden;
    }

    .tool-panel > summary {
      display: flex;
      align-items: center;
      min-height: 46px;
      padding: 0 14px;
      color: var(--text);
      font-weight: 750;
      cursor: pointer;
      list-style: none;
    }

    .tool-panel > summary::-webkit-details-marker {
      display: none;
    }

    .tool-panel > summary::after {
      content: "+";
      margin-left: auto;
      color: var(--muted);
      font-weight: 800;
    }

    .tool-panel[open] > summary::after {
      content: "-";
    }

    .tool-panel-content {
      padding: 0 14px 14px;
    }

    .position-editor {
      display: grid;
      grid-template-columns: minmax(160px, 220px) auto auto auto minmax(0, 1fr);
      gap: 12px;
      align-items: end;
      margin-top: 10px;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
    }

    select {
      width: 100%;
      min-height: 42px;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: var(--panel-soft);
      color: var(--text);
      padding: 0 10px;
      font: inherit;
    }

    input[type="number"] {
      width: 100%;
      min-height: 42px;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: var(--panel-soft);
      color: var(--text);
      padding: 0 10px;
      font: inherit;
    }

    .field {
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: 0.9rem;
      min-width: 0;
    }

    .field span {
      color: var(--text);
      font-weight: 650;
    }

    .fen {
      min-width: 0;
      overflow-wrap: anywhere;
      color: var(--muted);
      font: 0.82rem ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      line-height: 1.4;
    }

    .captures-panel {
      margin-top: 10px;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
    }

    .captures-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 10px;
    }

    .captures-header h2 {
      margin: 0;
      color: var(--text);
      font-size: 0.95rem;
      letter-spacing: 0;
    }

    .capture-list {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
      gap: 10px;
    }

    .capture-card {
      min-width: 0;
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-soft);
    }

    .capture-card img {
      display: block;
      width: 100%;
      aspect-ratio: 16 / 9;
      object-fit: cover;
      background: #080a0d;
    }

    .capture-body {
      display: grid;
      gap: 5px;
      padding: 10px;
      color: var(--muted);
      font-size: 0.82rem;
      overflow-wrap: anywhere;
    }

    .capture-body a {
      color: var(--text);
      font-weight: 700;
      text-decoration: none;
    }

    .capture-tools {
      display: grid;
      gap: 6px;
      padding-top: 4px;
    }

    .capture-tools summary {
      color: var(--text);
      cursor: pointer;
      font-weight: 750;
    }

    .capture-empty {
      color: var(--muted);
      font-size: 0.9rem;
    }

    .dataset-summary {
      display: grid;
      grid-template-columns: repeat(7, minmax(0, 1fr));
      gap: 10px;
    }

    .slider {
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: 0.9rem;
      min-width: 0;
    }

    .slider span {
      color: var(--text);
      font-weight: 650;
    }

    input[type="range"] {
      width: 100%;
      accent-color: var(--accent-strong);
    }

    @media (max-width: 720px) {
      main {
        width: min(100vw - 20px, 1180px);
        padding: 14px 0;
      }

      header,
      .toolbar {
        align-items: stretch;
        flex-direction: column;
      }

      .status,
      .message {
        text-align: left;
        white-space: normal;
      }

      .last-capture-header {
        align-items: flex-start;
        flex-direction: column;
      }

      button,
      .button-link {
        width: 100%;
      }

      .actions,
      .toggle {
        width: 100%;
      }

      .details {
        grid-template-columns: 1fr;
      }

      .camera-controls {
        grid-template-columns: 1fr;
      }

      .diagnostics {
        grid-template-columns: 1fr;
      }

      .calibration {
        grid-template-columns: 1fr;
      }

      .position-editor {
        grid-template-columns: 1fr;
      }

      .dataset-summary {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <h1>ChessV2 Camera</h1>
      <div class="status"><span id="status-dot" class="dot"></span><span id="status-text">Checking camera...</span></div>
    </header>

    <section class="viewer" aria-label="Camera stream">
      <img id="camera-stream" src="/stream.mjpg" alt="Live camera stream">
      <div id="board-overlay" class="board-overlay hidden" aria-hidden="true"></div>
    </section>

    <section class="toolbar">
      <div class="actions">
        <button id="snapshot-button" type="button">Save snapshot</button>
        <button id="hdr-snapshot-button" class="secondary" type="button">Save HDR snapshot</button>
        <button id="diagnostics-button" class="secondary" type="button">Refresh diagnostics</button>
      </div>
      <div id="message" class="message">Snapshots are saved on the Pi in captures/.</div>
    </section>

    <section id="last-capture-panel" class="last-capture-panel" aria-label="Last captured image" hidden>
      <div class="last-capture-header">
        <strong>Last capture</strong>
        <a id="last-capture-link" class="button-link secondary" href="#" target="_blank">Open image</a>
      </div>
      <img id="last-capture-image" alt="Last captured frame">
    </section>

    <section class="details" aria-label="Camera details">
      <div class="detail"><strong>Resolution</strong><span id="resolution">-</span></div>
      <div class="detail"><strong>Frame rate</strong><span id="fps">-</span></div>
      <div class="detail"><strong>Mode</strong><span id="mode-label">-</span></div>
      <div class="detail"><strong>FOV</strong><span id="fov-label">-</span></div>
      <div class="detail"><strong>Focus</strong><span id="focus-label">-</span></div>
      <div class="detail"><strong>Exposure</strong><span id="exposure-label">-</span></div>
      <div class="detail"><strong>Image tuning</strong><span id="tuning-label">-</span></div>
      <div class="detail"><strong>Focus hardware</strong><span id="focus-hardware">-</span></div>
      <div class="detail"><strong>Capture path</strong><span id="capture-path">-</span></div>
    </section>

    <section class="camera-controls" aria-label="Camera controls">
      <label class="field"><span>View mode</span><select id="camera-mode-select"></select></label>
      <label class="field"><span>Focus</span><select id="focus-mode-select">
        <option value="continuous">Continuous</option>
        <option value="auto">Auto</option>
        <option value="manual">Manual</option>
      </select></label>
      <label class="field"><span>AF range</span><select id="af-range-select">
        <option value="full">Full</option>
        <option value="normal">Normal</option>
        <option value="macro">Macro</option>
      </select></label>
      <label class="field"><span>AF speed</span><select id="af-speed-select">
        <option value="normal">Normal</option>
        <option value="fast">Fast</option>
      </select></label>
      <label class="field"><span>Color</span><select id="color-order-select">
        <option value="rgb">Normal RGB</option>
        <option value="bgr">Swap red/blue</option>
      </select></label>
      <label class="slider"><span>Manual focus <output id="lens-position-value">-</output></span><input id="lens-position-slider" type="range" min="0" max="32" step="0.1"></label>
      <button id="apply-camera-settings-button" class="secondary" type="button">Apply camera</button>
      <button id="autofocus-button" class="secondary" type="button">Autofocus now</button>
    </section>

    <section class="camera-controls tuning-controls" aria-label="Exposure and image tuning">
      <label class="field"><span>Exposure</span><select id="exposure-mode-select">
        <option value="auto">Auto</option>
        <option value="manual">Manual</option>
      </select></label>
      <label class="slider"><span>EV <output id="exposure-value-value">-</output></span><input id="exposure-value-slider" type="range" min="-4" max="4" step="0.1"></label>
      <label class="slider"><span>Exposure us <output id="exposure-time-value">-</output></span><input id="exposure-time-slider" type="range" min="100" max="250000" step="100"></label>
      <label class="slider"><span>Gain <output id="analogue-gain-value">-</output></span><input id="analogue-gain-slider" type="range" min="1" max="16" step="0.1"></label>
      <label class="slider"><span>Brightness <output id="brightness-value">-</output></span><input id="brightness-slider" type="range" min="-1" max="1" step="0.05"></label>
      <label class="slider"><span>Contrast <output id="contrast-value">-</output></span><input id="contrast-slider" type="range" min="0" max="4" step="0.05"></label>
      <label class="slider"><span>Saturation <output id="saturation-value">-</output></span><input id="saturation-slider" type="range" min="0" max="4" step="0.05"></label>
      <label class="slider"><span>Sharpness <output id="sharpness-value">-</output></span><input id="sharpness-slider" type="range" min="0" max="16" step="0.1"></label>
      <label class="slider"><span>Shadow lift <output id="shadow-lift-value">-</output></span><input id="shadow-lift-slider" type="range" min="0" max="1" step="0.05"></label>
      <label class="slider"><span>Purple fix <output id="purple-fix-value">-</output></span><input id="purple-fix-slider" type="range" min="0" max="1" step="0.05"></label>
    </section>

    <details class="tool-panel">
      <summary>Diagnostics</summary>
      <div class="tool-panel-content">
        <section class="diagnostics" aria-label="Camera diagnostics">
          <div class="detail"><strong>rpicam</strong><pre id="rpicam-output" class="diagnostic-output">-</pre></div>
          <div class="detail"><strong>Boot config</strong><pre id="config-output" class="diagnostic-output">-</pre></div>
          <div class="detail"><strong>Recommendation</strong><pre id="config-recommendation-output" class="diagnostic-output">-</pre></div>
        </section>
      </div>
    </details>

    <details class="tool-panel" id="recognition-tools">
      <summary>Chess recognition tools</summary>
      <div class="tool-panel-content">
        <section class="toolbar" aria-label="Recognition actions">
          <div class="actions">
            <button id="detect-live-button" class="secondary" type="button">Save + detect board</button>
            <button id="recognize-live-button" class="secondary" type="button">Save + recognize</button>
            <label class="toggle"><input id="grid-toggle" type="checkbox"> Board grid</label>
          </div>
        </section>

        <section aria-label="Pipeline readiness">
          <div class="readiness-header">
            <h2>Pipeline readiness</h2>
            <button id="refresh-readiness-button" class="secondary" type="button">Refresh readiness</button>
          </div>
          <div id="readiness-list" class="readiness-list"></div>
        </section>

        <section class="calibration" aria-label="Board calibration">
          <label class="slider"><span>Left <output id="left-value">-</output></span><input id="left-slider" type="range" min="0" max="95" step="0.25"></label>
          <label class="slider"><span>Top <output id="top-value">-</output></span><input id="top-slider" type="range" min="0" max="95" step="0.25"></label>
          <label class="slider"><span>Size <output id="size-value">-</output></span><input id="size-slider" type="range" min="5" max="95" step="0.25"></label>
          <label class="toggle"><input id="orientation-toggle" type="checkbox"> White at bottom</label>
          <button id="save-calibration-button" class="secondary" type="button">Save calibration</button>
        </section>

        <section class="position-editor" aria-label="Piece position editor">
          <label class="field"><span>Piece painter</span><select id="piece-select">
            <option value="">Empty square</option>
            <option value="wK">White king</option>
            <option value="wQ">White queen</option>
            <option value="wR">White rook</option>
            <option value="wB">White bishop</option>
            <option value="wN">White knight</option>
            <option value="wP">White pawn</option>
            <option value="bK">Black king</option>
            <option value="bQ">Black queen</option>
            <option value="bR">Black rook</option>
            <option value="bB">Black bishop</option>
            <option value="bN">Black knight</option>
            <option value="bP">Black pawn</option>
          </select></label>
          <button id="save-position-button" class="secondary" type="button">Save position</button>
          <button id="start-position-button" class="secondary" type="button">Starting position</button>
          <button id="clear-position-button" class="secondary" type="button">Clear board</button>
          <div class="field"><span>FEN</span><div id="fen-output" class="fen">-</div></div>
        </section>
      </div>
    </details>

    <section class="captures-panel" aria-label="Saved captures">
      <div class="captures-header">
        <h2>Saved captures</h2>
        <div class="actions">
          <button id="refresh-captures-button" class="secondary" type="button">Refresh captures</button>
          <button id="delete-all-captures-button" class="secondary" type="button">Delete all</button>
        </div>
      </div>
      <div id="capture-list" class="capture-list"></div>
    </section>

    <details class="tool-panel">
      <summary>Square crop dataset</summary>
      <div class="tool-panel-content">
        <section class="captures-panel" aria-label="Square crop dataset">
          <div class="captures-header">
            <h2>Square crop dataset</h2>
            <div class="actions">
              <button id="refresh-dataset-button" class="secondary" type="button">Refresh dataset</button>
              <a class="button-link secondary" href="/dataset.csv" download>Download CSV</a>
              <button id="train-occupancy-button" class="secondary" type="button">Train occupancy</button>
              <button id="train-piece-button" class="secondary" type="button">Train pieces</button>
              <button id="evaluate-piece-button" class="secondary" type="button">Evaluate pieces</button>
            </div>
          </div>
          <div class="dataset-summary">
            <div class="detail"><strong>Rows</strong><span id="dataset-rows">0</span></div>
            <div class="detail"><strong>Occupied</strong><span id="dataset-occupied">0</span></div>
            <div class="detail"><strong>Empty</strong><span id="dataset-empty">0</span></div>
            <div class="detail"><strong>Analyzed captures</strong><span id="dataset-captures">0</span></div>
            <div class="detail"><strong>Occupancy</strong><span id="occupancy-model-status">Not trained</span></div>
            <div class="detail"><strong>Pieces</strong><span id="piece-model-status">Not trained</span></div>
            <div class="detail"><strong>Piece eval</strong><span id="piece-evaluation-status">Not run</span></div>
          </div>
          <div class="detail" style="margin-top: 10px;"><strong>Labels</strong><span id="dataset-labels">-</span></div>
        </section>
      </div>
    </details>
  </main>

  <script>
    const dot = document.getElementById("status-dot");
    const statusText = document.getElementById("status-text");
    const message = document.getElementById("message");
    const viewer = document.querySelector(".viewer");
    const streamImage = document.getElementById("camera-stream");
    const button = document.getElementById("snapshot-button");
    const hdrButton = document.getElementById("hdr-snapshot-button");
    const lastCapturePanel = document.getElementById("last-capture-panel");
    const lastCaptureImage = document.getElementById("last-capture-image");
    const lastCaptureLink = document.getElementById("last-capture-link");
    const detectLiveButton = document.getElementById("detect-live-button");
    const recognizeLiveButton = document.getElementById("recognize-live-button");
    const diagnosticsButton = document.getElementById("diagnostics-button");
    const gridToggle = document.getElementById("grid-toggle");
    const boardOverlay = document.getElementById("board-overlay");
    const resolution = document.getElementById("resolution");
    const fps = document.getElementById("fps");
    const modeLabel = document.getElementById("mode-label");
    const fovLabel = document.getElementById("fov-label");
    const focusLabel = document.getElementById("focus-label");
    const exposureLabel = document.getElementById("exposure-label");
    const tuningLabel = document.getElementById("tuning-label");
    const focusHardware = document.getElementById("focus-hardware");
    const capturePath = document.getElementById("capture-path");
    const cameraModeSelect = document.getElementById("camera-mode-select");
    const focusModeSelect = document.getElementById("focus-mode-select");
    const afRangeSelect = document.getElementById("af-range-select");
    const afSpeedSelect = document.getElementById("af-speed-select");
    const colorOrderSelect = document.getElementById("color-order-select");
    const lensPositionSlider = document.getElementById("lens-position-slider");
    const lensPositionValue = document.getElementById("lens-position-value");
    const applyCameraSettingsButton = document.getElementById("apply-camera-settings-button");
    const autofocusButton = document.getElementById("autofocus-button");
    const exposureModeSelect = document.getElementById("exposure-mode-select");
    const exposureValueSlider = document.getElementById("exposure-value-slider");
    const exposureValueValue = document.getElementById("exposure-value-value");
    const exposureTimeSlider = document.getElementById("exposure-time-slider");
    const exposureTimeValue = document.getElementById("exposure-time-value");
    const analogueGainSlider = document.getElementById("analogue-gain-slider");
    const analogueGainValue = document.getElementById("analogue-gain-value");
    const brightnessSlider = document.getElementById("brightness-slider");
    const brightnessValue = document.getElementById("brightness-value");
    const contrastSlider = document.getElementById("contrast-slider");
    const contrastValue = document.getElementById("contrast-value");
    const saturationSlider = document.getElementById("saturation-slider");
    const saturationValue = document.getElementById("saturation-value");
    const sharpnessSlider = document.getElementById("sharpness-slider");
    const sharpnessValue = document.getElementById("sharpness-value");
    const shadowLiftSlider = document.getElementById("shadow-lift-slider");
    const shadowLiftValue = document.getElementById("shadow-lift-value");
    const purpleFixSlider = document.getElementById("purple-fix-slider");
    const purpleFixValue = document.getElementById("purple-fix-value");
    const refreshReadinessButton = document.getElementById("refresh-readiness-button");
    const readinessList = document.getElementById("readiness-list");
    const rpicamOutput = document.getElementById("rpicam-output");
    const configOutput = document.getElementById("config-output");
    const configRecommendationOutput = document.getElementById("config-recommendation-output");
    const leftSlider = document.getElementById("left-slider");
    const topSlider = document.getElementById("top-slider");
    const sizeSlider = document.getElementById("size-slider");
    const orientationToggle = document.getElementById("orientation-toggle");
    const leftValue = document.getElementById("left-value");
    const topValue = document.getElementById("top-value");
    const sizeValue = document.getElementById("size-value");
    const saveCalibrationButton = document.getElementById("save-calibration-button");
    const pieceSelect = document.getElementById("piece-select");
    const savePositionButton = document.getElementById("save-position-button");
    const startPositionButton = document.getElementById("start-position-button");
    const clearPositionButton = document.getElementById("clear-position-button");
    const fenOutput = document.getElementById("fen-output");
    const refreshCapturesButton = document.getElementById("refresh-captures-button");
    const deleteAllCapturesButton = document.getElementById("delete-all-captures-button");
    const captureList = document.getElementById("capture-list");
    const refreshDatasetButton = document.getElementById("refresh-dataset-button");
    const datasetRows = document.getElementById("dataset-rows");
    const datasetOccupied = document.getElementById("dataset-occupied");
    const datasetEmpty = document.getElementById("dataset-empty");
    const datasetCaptures = document.getElementById("dataset-captures");
    const datasetLabels = document.getElementById("dataset-labels");
    const trainOccupancyButton = document.getElementById("train-occupancy-button");
    const occupancyModelStatus = document.getElementById("occupancy-model-status");
    const trainPieceButton = document.getElementById("train-piece-button");
    const pieceModelStatus = document.getElementById("piece-model-status");
    const evaluatePieceButton = document.getElementById("evaluate-piece-button");
    const pieceEvaluationStatus = document.getElementById("piece-evaluation-status");
    const files = ["a", "b", "c", "d", "e", "f", "g", "h"];
    const pieceFen = { wK: "K", wQ: "Q", wR: "R", wB: "B", wN: "N", wP: "P", bK: "k", bQ: "q", bR: "r", bB: "b", bN: "n", bP: "p" };
    const defaultCalibration = { left: 26.5, top: 8, size: 47.25, white_bottom: true };
    let imageAspect = 16 / 9;
    let boardPosition = {};
    let fen = "-";
    let focusApplyTimer = null;

    function numeric(value, fallback) {
      const number = Number(value);
      return Number.isFinite(number) ? number : fallback;
    }

    function wait(ms) {
      return new Promise((resolve) => {
        window.setTimeout(resolve, ms);
      });
    }

    function signedFixed(value, digits = 1) {
      const number = numeric(value, 0);
      return `${number > 0 ? "+" : ""}${number.toFixed(digits)}`;
    }

    function fixed(value, digits = 2) {
      return numeric(value, 0).toFixed(digits);
    }

    function wholeNumber(value) {
      return Math.round(numeric(value, 0)).toLocaleString();
    }

    function updateTuningOutputs() {
      exposureValueValue.textContent = signedFixed(exposureValueSlider.value, 1);
      exposureTimeValue.textContent = wholeNumber(exposureTimeSlider.value);
      analogueGainValue.textContent = fixed(analogueGainSlider.value, 1);
      brightnessValue.textContent = signedFixed(brightnessSlider.value, 2);
      contrastValue.textContent = fixed(contrastSlider.value, 2);
      saturationValue.textContent = fixed(saturationSlider.value, 2);
      sharpnessValue.textContent = fixed(sharpnessSlider.value, 1);
      shadowLiftValue.textContent = fixed(shadowLiftSlider.value, 2);
      purpleFixValue.textContent = fixed(purpleFixSlider.value, 2);
      exposureValueSlider.disabled = exposureModeSelect.value !== "auto";
      exposureTimeSlider.disabled = exposureModeSelect.value !== "manual";
      analogueGainSlider.disabled = exposureModeSelect.value !== "manual";
    }

    function currentCalibration() {
      return {
        left: numeric(leftSlider.value, defaultCalibration.left),
        top: numeric(topSlider.value, defaultCalibration.top),
        size: numeric(sizeSlider.value, defaultCalibration.size),
        white_bottom: orientationToggle.checked,
      };
    }

    function currentCameraSettings() {
      return {
        mode_id: cameraModeSelect.value || "hd",
        focus_mode: focusModeSelect.value || "continuous",
        af_range: afRangeSelect.value || "full",
        af_speed: afSpeedSelect.value || "normal",
        color_order: colorOrderSelect.value || "rgb",
        lens_position: numeric(lensPositionSlider.value, 1),
        exposure_mode: exposureModeSelect.value || "auto",
        exposure_value: numeric(exposureValueSlider.value, 0),
        exposure_time_us: numeric(exposureTimeSlider.value, 30000),
        analogue_gain: numeric(analogueGainSlider.value, 2),
        brightness: numeric(brightnessSlider.value, 0),
        contrast: numeric(contrastSlider.value, 1),
        saturation: numeric(saturationSlider.value, 1),
        sharpness: numeric(sharpnessSlider.value, 1),
        shadow_lift: numeric(shadowLiftSlider.value, 0),
        purple_fix: numeric(purpleFixSlider.value, 0),
      };
    }

    function renderCameraModeOptions(modes, selectedMode) {
      cameraModeSelect.replaceChildren();
      for (const mode of modes || []) {
        const option = document.createElement("option");
        const size = mode.resolution || [];
        option.value = mode.id;
        option.textContent = `${mode.label} ${size.join(" x ")} ${mode.fps} fps`;
        cameraModeSelect.appendChild(option);
      }
      cameraModeSelect.value = selectedMode || "hd";
    }

    function applyCameraStatus(status, syncCalibration = false, syncControls = false) {
      const size = status.resolution || [1280, 720];
      const width = numeric(size[0], 1280);
      const height = numeric(size[1], 720);
      imageAspect = width / height;
      viewer.style.aspectRatio = `${width} / ${height}`;
      resolution.textContent = `${width} x ${height}`;
      fps.textContent = `${status.stream_fps} fps`;
      modeLabel.textContent = status.mode_label || status.mode_id || "-";
      fovLabel.textContent = status.fov === "full" ? "Full sensor" : "Cropped";
      focusLabel.textContent = status.focus_mode || "-";
      if (status.exposure_mode === "manual") {
        exposureLabel.textContent = `${wholeNumber(status.exposure_time_us)} us, ${fixed(status.analogue_gain, 1)}x`;
      } else {
        exposureLabel.textContent = `Auto EV ${signedFixed(status.exposure_value, 1)}`;
      }
      tuningLabel.textContent = `Lift ${fixed(status.shadow_lift, 2)}, purple ${fixed(status.purple_fix, 2)}`;
      const v4l2 = status.v4l2_focus || {};
      const focusParts = [];
      if (status.focus_algorithm_available) {
        focusParts.push("AF algorithm");
      } else {
        focusParts.push("no AF algorithm");
      }
      if (v4l2.available) {
        focusParts.push(`V4L2 ${v4l2.value ?? "-"}`);
      }
      focusHardware.textContent = focusParts.join(", ");
      capturePath.textContent = status.captures_dir || "-";
      if (syncControls) {
        renderCameraModeOptions(status.camera_modes || [], status.mode_id);
        focusModeSelect.value = status.focus_mode || "continuous";
        afRangeSelect.value = status.af_range || "full";
        afSpeedSelect.value = status.af_speed || "normal";
        colorOrderSelect.value = status.color_order || "rgb";
        lensPositionSlider.value = numeric(status.lens_position, 1);
        lensPositionValue.textContent = Number(lensPositionSlider.value).toFixed(1);
        lensPositionSlider.disabled = focusModeSelect.value !== "manual";
        exposureModeSelect.value = status.exposure_mode || "auto";
        exposureValueSlider.value = numeric(status.exposure_value, 0);
        exposureTimeSlider.value = numeric(status.exposure_time_us, 30000);
        analogueGainSlider.value = numeric(status.analogue_gain, 2);
        brightnessSlider.value = numeric(status.brightness, 0);
        contrastSlider.value = numeric(status.contrast, 1);
        saturationSlider.value = numeric(status.saturation, 1);
        sharpnessSlider.value = numeric(status.sharpness, 1);
        shadowLiftSlider.value = numeric(status.shadow_lift, 0);
        purpleFixSlider.value = numeric(status.purple_fix, 0);
        updateTuningOutputs();
      }
      if (syncCalibration) {
        applyCalibration(status.calibration || currentCalibration());
      }
    }

    function squareName(row, col, whiteBottom) {
      const file = whiteBottom ? files[col] : files[7 - col];
      const rank = whiteBottom ? 8 - row : row + 1;
      return `${file}${rank}`;
    }

    function renderSquareLabels(calibration) {
      boardOverlay.replaceChildren();
      for (let row = 0; row < 8; row += 1) {
        for (let col = 0; col < 8; col += 1) {
          const square = squareName(row, col, Boolean(calibration.white_bottom));
          const piece = boardPosition[square] || "";
          const label = document.createElement("span");
          const squareText = document.createElement("span");
          label.className = piece ? "square-label has-piece" : "square-label";
          label.dataset.square = square;
          squareText.className = "square-name";
          squareText.textContent = square;
          label.appendChild(squareText);
          if (piece) {
            const pieceText = document.createElement("span");
            pieceText.className = "piece-code";
            pieceText.textContent = piece;
            label.appendChild(pieceText);
          }
          label.addEventListener("click", () => {
            setPiece(square, pieceSelect.value);
          });
          boardOverlay.appendChild(label);
        }
      }
    }

    function applyCalibration(calibration) {
      const maxSize = Math.min(95, 100 / imageAspect);
      const size = Math.min(Math.max(numeric(calibration.size, defaultCalibration.size), 5), maxSize);
      const maxLeft = 100 - size;
      const maxTop = 100 - (size * imageAspect);
      const left = Math.min(Math.max(numeric(calibration.left, defaultCalibration.left), 0), maxLeft);
      const top = Math.min(Math.max(numeric(calibration.top, defaultCalibration.top), 0), maxTop);
      const whiteBottom = Boolean(calibration.white_bottom);
      leftSlider.max = maxLeft.toFixed(2);
      topSlider.max = maxTop.toFixed(2);
      sizeSlider.max = maxSize.toFixed(2);
      leftSlider.value = left;
      topSlider.value = top;
      sizeSlider.value = size;
      orientationToggle.checked = whiteBottom;
      leftValue.textContent = `${left.toFixed(2)}%`;
      topValue.textContent = `${top.toFixed(2)}%`;
      sizeValue.textContent = `${size.toFixed(2)}%`;
      boardOverlay.style.setProperty("--board-left", left);
      boardOverlay.style.setProperty("--board-top", top);
      boardOverlay.style.setProperty("--board-size", size);
      renderSquareLabels({ white_bottom: whiteBottom });
    }

    function setPiece(square, piece) {
      if (piece) {
        boardPosition[square] = piece;
      } else {
        delete boardPosition[square];
      }
      fen = positionToFen(boardPosition);
      fenOutput.textContent = `${fen} (unsaved)`;
      applyCalibration(currentCalibration());
    }

    function positionToFen(position) {
      const ranks = [];
      for (let rank = 8; rank >= 1; rank -= 1) {
        let empty = 0;
        let rankText = "";
        for (const file of files) {
          const piece = position[`${file}${rank}`];
          if (!piece) {
            empty += 1;
            continue;
          }
          if (empty) {
            rankText += String(empty);
            empty = 0;
          }
          rankText += pieceFen[piece] || "1";
        }
        if (empty) {
          rankText += String(empty);
        }
        ranks.push(rankText);
      }
      return `${ranks.join("/")} w - - 0 1`;
    }

    function loadBoardPosition(pieces, fenText, calibration, label) {
      boardPosition = { ...(pieces || {}) };
      fen = fenText || positionToFen(boardPosition);
      fenOutput.textContent = `${fen} (loaded)`;
      applyCalibration(calibration || currentCalibration());
      message.textContent = label || `Loaded ${Object.keys(boardPosition).length} labels.`;
    }

    async function refreshHealth() {
      try {
        const response = await fetch("/health", { cache: "no-store" });
        const health = await response.json();
        dot.classList.toggle("ready", Boolean(health.camera_available));
        statusText.textContent = health.camera_available ? "Camera ready" : "Camera not detected";
        applyCameraStatus(health);
        if (!health.camera_available && health.last_error) {
          message.textContent = health.last_error;
        }
      } catch (error) {
        dot.classList.remove("ready");
        statusText.textContent = "Server unreachable";
        message.textContent = error.message;
      }
    }

    async function refreshCameraSettings() {
      try {
        const response = await fetch("/camera-settings", { cache: "no-store" });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || "Camera settings failed.");
        }
        applyCameraStatus(payload, true, true);
      } catch (error) {
        message.textContent = error.message;
      }
    }

    async function applyCameraSettings(options = {}) {
      const silent = Boolean(options.silent);
      applyCameraSettingsButton.disabled = true;
      if (!silent) {
        message.textContent = "Applying camera settings...";
      }
      try {
        const response = await fetch("/camera-settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(currentCameraSettings()),
        });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || "Camera settings failed.");
        }
        applyCameraStatus(payload.health || payload.settings || {}, true, true);
        streamImage.src = `/stream.mjpg?ts=${Date.now()}`;
        if (!silent) {
          message.textContent = `Camera set to ${(payload.settings || {}).mode_label || cameraModeSelect.value}.`;
        }
        return payload;
      } catch (error) {
        message.textContent = error.message;
        throw error;
      } finally {
        applyCameraSettingsButton.disabled = false;
        refreshHealth();
      }
    }

    async function applyFocusSettings(extra = {}) {
      const payload = { ...currentCameraSettings(), ...extra };
      try {
        const response = await fetch("/focus", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const result = await response.json();
        if (!response.ok) {
          throw new Error(result.error || "Focus update failed.");
        }
        focusLabel.textContent = (result.settings || {}).focus_mode || payload.focus_mode;
        const v4l2 = (result.focus_status || {}).v4l2_focus || {};
        if (v4l2.available) {
          focusHardware.textContent = `manual V4L2 ${v4l2.value ?? v4l2.requested_value ?? "-"}`;
        } else if ((result.focus_status || {}).libcamera_focus_algorithm === false) {
          focusHardware.textContent = "no AF algorithm";
        }
        message.textContent = payload.focus_mode === "manual"
          ? `Manual focus ${Number(payload.lens_position).toFixed(1)}`
          : `Focus set to ${payload.focus_mode}.`;
        return result;
      } catch (error) {
        message.textContent = error.message;
        return null;
      }
    }

    function scheduleManualFocusApply() {
      clearTimeout(focusApplyTimer);
      focusApplyTimer = setTimeout(() => {
        if (focusModeSelect.value === "manual") {
          applyFocusSettings();
        }
      }, 250);
    }

    async function triggerAutofocus() {
      autofocusButton.disabled = true;
      message.textContent = "Autofocus running...";
      try {
        const response = await fetch("/focus/trigger", { method: "POST" });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || "Autofocus failed.");
        }
        await refreshCameraSettings();
        message.textContent = "Autofocus triggered.";
      } catch (error) {
        message.textContent = error.message;
      } finally {
        autofocusButton.disabled = false;
      }
    }

    function renderReadiness(payload) {
      readinessList.replaceChildren();
      for (const gate of payload.gates || []) {
        const item = document.createElement("div");
        const label = document.createElement("strong");
        const detail = document.createElement("div");
        item.className = `readiness-item ${gate.ready ? "ready" : "waiting"}`;
        label.textContent = `${gate.label}: ${gate.ready ? "Ready" : "Waiting"}`;
        detail.textContent = gate.detail || "-";
        item.appendChild(label);
        item.appendChild(detail);
        if (!gate.ready && gate.action) {
          const action = document.createElement("div");
          action.className = "readiness-action";
          action.textContent = gate.action;
          item.appendChild(action);
        }
        readinessList.appendChild(item);
      }
      if (payload.next_action) {
        message.textContent = payload.ready ? "Recognition pipeline ready." : payload.next_action;
      }
    }

    async function refreshReadiness() {
      refreshReadinessButton.disabled = true;
      try {
        const response = await fetch("/readiness", { cache: "no-store" });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || "Readiness check failed.");
        }
        renderReadiness(payload);
      } catch (error) {
        readinessList.replaceChildren();
        const item = document.createElement("div");
        item.className = "readiness-item waiting";
        item.textContent = error.message;
        readinessList.appendChild(item);
      } finally {
        refreshReadinessButton.disabled = false;
      }
    }

    async function refreshDiagnostics() {
      diagnosticsButton.disabled = true;
      try {
        const response = await fetch("/diagnostics", { cache: "no-store" });
        const diagnostics = await response.json();
        const rpicam = diagnostics.rpicam || {};
        rpicamOutput.textContent = rpicam.output || "-";
        configOutput.textContent = (diagnostics.config_lines || []).join("\\n") || "-";
        const recommendation = diagnostics.config_recommendation || {};
        configRecommendationOutput.textContent = recommendation.message || "-";
      } catch (error) {
        rpicamOutput.textContent = error.message;
        configOutput.textContent = "-";
        configRecommendationOutput.textContent = "-";
      } finally {
        diagnosticsButton.disabled = false;
      }
    }

    async function refreshCalibration() {
      try {
        const response = await fetch("/calibration", { cache: "no-store" });
        const payload = await response.json();
        applyCalibration(payload.calibration);
      } catch (error) {
        message.textContent = error.message;
      }
    }

    async function saveCalibration() {
      saveCalibrationButton.disabled = true;
      try {
        const response = await fetch("/calibration", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(currentCalibration()),
        });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || "Calibration failed.");
        }
        applyCalibration(payload.calibration);
        message.textContent = "Calibration saved.";
      } catch (error) {
        message.textContent = error.message;
      } finally {
        saveCalibrationButton.disabled = false;
        refreshReadiness();
      }
    }

    async function refreshPosition() {
      try {
        const response = await fetch("/position", { cache: "no-store" });
        const payload = await response.json();
        boardPosition = payload.pieces || {};
        fen = payload.fen || "-";
        fenOutput.textContent = fen;
        applyCalibration(currentCalibration());
      } catch (error) {
        message.textContent = error.message;
      }
    }

    async function savePosition(pieces = boardPosition) {
      savePositionButton.disabled = true;
      try {
        const response = await fetch("/position", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ pieces }),
        });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || "Position save failed.");
        }
        boardPosition = payload.pieces || {};
        fen = payload.fen || "-";
        fenOutput.textContent = fen;
        applyCalibration(currentCalibration());
        message.textContent = "Position saved.";
      } catch (error) {
        message.textContent = error.message;
      } finally {
        savePositionButton.disabled = false;
      }
    }

    async function usePositionPreset(path, label) {
      [startPositionButton, clearPositionButton].forEach((control) => {
        control.disabled = true;
      });
      try {
        const response = await fetch(path, { method: "POST" });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || "Position update failed.");
        }
        boardPosition = payload.pieces || {};
        fen = payload.fen || "-";
        fenOutput.textContent = fen;
        applyCalibration(currentCalibration());
        message.textContent = label;
      } catch (error) {
        message.textContent = error.message;
      } finally {
        [startPositionButton, clearPositionButton].forEach((control) => {
          control.disabled = false;
        });
      }
    }

    function renderCaptures(captures) {
      captureList.replaceChildren();
      if (!captures.length) {
        const empty = document.createElement("div");
        empty.className = "capture-empty";
        empty.textContent = "No snapshots saved yet.";
        captureList.appendChild(empty);
        return;
      }

      for (const capture of captures) {
        const card = document.createElement("article");
        const image = document.createElement("img");
        const body = document.createElement("div");
        const imageLink = document.createElement("a");
        const metaLink = document.createElement("a");
        const fenLine = document.createElement("div");
        const piecesLine = document.createElement("div");
        const labelsLine = document.createElement("div");
        const cropsLine = document.createElement("div");
        const analysisLine = document.createElement("div");
        const predictionLine = document.createElement("div");
        const piecePredictionLine = document.createElement("div");
        const recognitionLine = document.createElement("div");
        const boardLine = document.createElement("div");
        const boardDebugLink = document.createElement("a");
        const loadLabelsButton = document.createElement("button");
        const loadPredictionsButton = document.createElement("button");
        const applyLabelsButton = document.createElement("button");
        const saveSampleButton = document.createElement("button");
        const detectBoardButton = document.createElement("button");
        const exportButton = document.createElement("button");
        const analyzeButton = document.createElement("button");
        const predictButton = document.createElement("button");
        const predictPiecesButton = document.createElement("button");
        const recognizePositionButton = document.createElement("button");
        const deleteButton = document.createElement("button");
        const chessTools = document.createElement("details");
        const chessToolsSummary = document.createElement("summary");

        card.className = "capture-card";
        image.src = capture.url;
        image.alt = capture.filename;
        body.className = "capture-body";
        chessTools.className = "capture-tools";
        chessToolsSummary.textContent = "Chess tools";
        chessTools.appendChild(chessToolsSummary);
        imageLink.href = capture.url;
        imageLink.textContent = capture.filename;
        imageLink.target = "_blank";
        metaLink.href = capture.metadata_url || capture.url;
        metaLink.textContent = capture.metadata_url ? "metadata" : "no metadata";
        metaLink.target = "_blank";
        fenLine.textContent = capture.fen || "FEN unavailable";
        piecesLine.textContent = `${Object.keys(capture.pieces || {}).length} saved pieces`;
        labelsLine.textContent = capture.labels_updated_at ? `Labels updated ${capture.labels_updated_at}` : "Labels not updated after capture";
        cropsLine.textContent = capture.square_crops_count ? `${capture.square_crops_count} square crops exported` : "No square crops exported";
        analysisLine.textContent = capture.square_analysis_at ? `Analyzed ${capture.square_analysis_at}` : "No square analysis yet";
        predictionLine.textContent = capture.occupancy_predictions_at ? `Predicted ${capture.occupancy_predictions_at}` : "No occupancy predictions yet";
        piecePredictionLine.textContent = capture.piece_predictions_at ? `${capture.predicted_piece_count || 0} pieces predicted` : "No piece predictions yet";
        recognitionLine.textContent = capture.position_recognized_at ? `Position recognized (${capture.recognition_average_confidence ?? "-"} avg confidence)` : "No position recognition yet";
        boardLine.textContent = capture.board_detected_at ? `Board detected ${capture.board_detected_at}` : "Board not auto-detected yet";
        boardDebugLink.textContent = capture.board_detection_debug_url ? "board detection debug" : "no board debug";
        if (capture.board_detection_debug_url) {
          boardDebugLink.href = capture.board_detection_debug_url;
          boardDebugLink.target = "_blank";
        }
        loadLabelsButton.className = "secondary";
        loadLabelsButton.type = "button";
        loadLabelsButton.textContent = "Load saved labels";
        loadLabelsButton.addEventListener("click", () => {
          loadBoardPosition(capture.pieces || {}, capture.fen, capture.calibration, `Loaded saved labels from ${capture.filename}.`);
        });
        loadPredictionsButton.className = "secondary";
        loadPredictionsButton.type = "button";
        loadPredictionsButton.textContent = "Load predictions";
        loadPredictionsButton.disabled = !Object.keys(capture.predicted_pieces || {}).length;
        loadPredictionsButton.addEventListener("click", () => {
          loadBoardPosition(
            capture.predicted_pieces || {},
            capture.predicted_fen,
            capture.calibration,
            `Loaded predicted labels from ${capture.filename}.`
          );
        });
        applyLabelsButton.className = "secondary";
        applyLabelsButton.type = "button";
        applyLabelsButton.textContent = "Apply current labels";
        applyLabelsButton.addEventListener("click", () => applyLabelsToCapture(capture.filename, applyLabelsButton));
        saveSampleButton.className = "secondary";
        saveSampleButton.type = "button";
        saveSampleButton.textContent = "Save labeled sample";
        saveSampleButton.addEventListener("click", () => saveLabeledSample(capture.filename, saveSampleButton));
        detectBoardButton.className = "secondary";
        detectBoardButton.type = "button";
        detectBoardButton.textContent = "Detect board";
        detectBoardButton.addEventListener("click", () => detectBoard(capture.filename, detectBoardButton));
        exportButton.className = "secondary";
        exportButton.type = "button";
        exportButton.textContent = "Export square crops";
        exportButton.addEventListener("click", () => exportSquareCrops(capture.filename, exportButton));
        analyzeButton.className = "secondary";
        analyzeButton.type = "button";
        analyzeButton.textContent = "Analyze square crops";
        analyzeButton.addEventListener("click", () => analyzeSquareCrops(capture.filename, analyzeButton));
        predictButton.className = "secondary";
        predictButton.type = "button";
        predictButton.textContent = "Predict occupancy";
        predictButton.addEventListener("click", () => predictOccupancy(capture.filename, predictButton));
        predictPiecesButton.className = "secondary";
        predictPiecesButton.type = "button";
        predictPiecesButton.textContent = "Predict pieces";
        predictPiecesButton.addEventListener("click", () => predictPieces(capture.filename, predictPiecesButton));
        recognizePositionButton.className = "secondary";
        recognizePositionButton.type = "button";
        recognizePositionButton.textContent = "Recognize position";
        recognizePositionButton.addEventListener("click", () => recognizePosition(capture.filename, recognizePositionButton));
        deleteButton.className = "secondary";
        deleteButton.type = "button";
        deleteButton.textContent = "Delete";
        deleteButton.addEventListener("click", () => deleteCapture(capture.filename, deleteButton));

        card.appendChild(image);
        body.appendChild(imageLink);
        body.appendChild(metaLink);
        body.appendChild(deleteButton);
        chessTools.appendChild(fenLine);
        chessTools.appendChild(piecesLine);
        chessTools.appendChild(labelsLine);
        chessTools.appendChild(cropsLine);
        chessTools.appendChild(analysisLine);
        chessTools.appendChild(predictionLine);
        chessTools.appendChild(piecePredictionLine);
        chessTools.appendChild(recognitionLine);
        chessTools.appendChild(boardLine);
        chessTools.appendChild(boardDebugLink);
        chessTools.appendChild(loadLabelsButton);
        chessTools.appendChild(loadPredictionsButton);
        chessTools.appendChild(applyLabelsButton);
        chessTools.appendChild(saveSampleButton);
        chessTools.appendChild(detectBoardButton);
        chessTools.appendChild(exportButton);
        chessTools.appendChild(analyzeButton);
        chessTools.appendChild(predictButton);
        chessTools.appendChild(predictPiecesButton);
        chessTools.appendChild(recognizePositionButton);
        body.appendChild(chessTools);
        card.appendChild(body);
        captureList.appendChild(card);
      }
    }

    async function refreshCaptures() {
      refreshCapturesButton.disabled = true;
      try {
        const response = await fetch("/captures", { cache: "no-store" });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || "Capture refresh failed.");
        }
        renderCaptures(payload.captures || []);
      } catch (error) {
        captureList.replaceChildren();
        const item = document.createElement("div");
        item.className = "capture-empty";
        item.textContent = error.message;
        captureList.appendChild(item);
      } finally {
        refreshCapturesButton.disabled = false;
      }
    }

    async function deleteCapture(filename, control) {
      if (!confirm(`Delete ${filename}?`)) {
        return;
      }
      control.disabled = true;
      try {
        const response = await fetch(`/captures/${encodeURIComponent(filename)}`, { method: "DELETE" });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || "Delete failed.");
        }
        renderCaptures(payload.captures || []);
        renderDataset(payload.dataset || {});
        message.textContent = `Deleted ${filename}.`;
      } catch (error) {
        message.textContent = error.message;
      } finally {
        control.disabled = false;
        refreshReadiness();
      }
    }

    async function deleteAllCaptures() {
      if (!confirm("Delete all saved snapshots and square crops?")) {
        return;
      }
      deleteAllCapturesButton.disabled = true;
      try {
        const response = await fetch("/captures", { method: "DELETE" });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || "Delete all failed.");
        }
        renderCaptures(payload.captures || []);
        renderDataset(payload.dataset || {});
        message.textContent = `Deleted ${payload.deleted_count || 0} capture files.`;
      } catch (error) {
        message.textContent = error.message;
      } finally {
        deleteAllCapturesButton.disabled = false;
        refreshReadiness();
      }
    }

    function renderDataset(dataset) {
      const summary = dataset.summary || {};
      const pieceCounts = summary.piece_counts || {};
      datasetRows.textContent = summary.rows || 0;
      datasetOccupied.textContent = summary.occupied_rows || 0;
      datasetEmpty.textContent = summary.empty_rows || 0;
      datasetCaptures.textContent = `${summary.analyzed_captures || 0} / ${summary.captures_with_crops || 0}`;
      datasetLabels.textContent = Object.entries(pieceCounts).map(([label, count]) => `${label}:${count}`).join(", ") || "-";
    }

    async function refreshDataset() {
      refreshDatasetButton.disabled = true;
      try {
        const response = await fetch("/dataset", { cache: "no-store" });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || "Dataset refresh failed.");
        }
        renderDataset(payload);
      } catch (error) {
        datasetLabels.textContent = error.message;
      } finally {
        refreshDatasetButton.disabled = false;
      }
    }

    async function refreshOccupancyModel() {
      try {
        const response = await fetch("/model/occupancy", { cache: "no-store" });
        const payload = await response.json();
        if (payload.available && payload.model) {
          const training = payload.model.training || {};
          occupancyModelStatus.textContent = `${training.rows || 0} rows (${training.occupied_rows || 0} occupied)`;
        } else {
          occupancyModelStatus.textContent = "Not trained";
        }
      } catch (error) {
        occupancyModelStatus.textContent = error.message;
      }
    }

    async function refreshPieceModel() {
      try {
        const response = await fetch("/model/piece", { cache: "no-store" });
        const payload = await response.json();
        if (payload.available && payload.model) {
          const training = payload.model.training || {};
          const labels = Object.keys(training.labels || {}).length;
          pieceModelStatus.textContent = `${training.rows || 0} rows (${labels} labels)`;
        } else {
          pieceModelStatus.textContent = "Not trained";
        }
      } catch (error) {
        pieceModelStatus.textContent = error.message;
      }
    }

    async function trainOccupancyModel() {
      trainOccupancyButton.disabled = true;
      try {
        const response = await fetch("/model/occupancy/train", { method: "POST" });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || "Training failed.");
        }
        const training = payload.model.training || {};
        occupancyModelStatus.textContent = `${training.rows || 0} rows (${training.occupied_rows || 0} occupied)`;
        message.textContent = "Occupancy model trained.";
      } catch (error) {
        message.textContent = error.message;
      } finally {
        trainOccupancyButton.disabled = false;
        refreshReadiness();
      }
    }

    async function trainPieceModel() {
      trainPieceButton.disabled = true;
      try {
        const response = await fetch("/model/piece/train", { method: "POST" });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || "Training failed.");
        }
        const training = payload.model.training || {};
        const labels = Object.keys(training.labels || {}).length;
        pieceModelStatus.textContent = `${training.rows || 0} rows (${labels} labels)`;
        pieceEvaluationStatus.textContent = "Not run";
        message.textContent = "Piece model trained.";
      } catch (error) {
        message.textContent = error.message;
      } finally {
        trainPieceButton.disabled = false;
        refreshReadiness();
      }
    }

    async function evaluatePieceModel() {
      evaluatePieceButton.disabled = true;
      try {
        const response = await fetch("/model/piece/evaluate", { cache: "no-store" });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || "Evaluation failed.");
        }
        const summary = payload.summary || {};
        const training = summary.training_accuracy == null ? "-" : `${Math.round(summary.training_accuracy * 100)}%`;
        const loo = summary.leave_one_out_accuracy == null ? "-" : `${Math.round(summary.leave_one_out_accuracy * 100)}%`;
        pieceEvaluationStatus.textContent = `train ${training}, LOO ${loo}`;
        message.textContent = `Piece eval: ${summary.rows || 0} rows, ${summary.leave_one_out_skipped_rows || 0} skipped.`;
      } catch (error) {
        pieceEvaluationStatus.textContent = error.message;
        message.textContent = error.message;
      } finally {
        evaluatePieceButton.disabled = false;
      }
    }

    async function applyLabelsToCapture(filename, control) {
      control.disabled = true;
      try {
        const response = await fetch(`/captures/${encodeURIComponent(filename)}/labels`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ pieces: boardPosition }),
        });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || "Label update failed.");
        }
        renderCaptures(payload.captures || []);
        refreshDataset();
        const count = Object.keys(payload.pieces || {}).length;
        message.textContent = `Applied ${count} labels to ${filename}.`;
      } catch (error) {
        message.textContent = error.message;
      } finally {
        control.disabled = false;
        refreshReadiness();
      }
    }

    async function saveLabeledSample(filename, control) {
      control.disabled = true;
      try {
        const response = await fetch(`/captures/${encodeURIComponent(filename)}/save-labeled-sample`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ pieces: boardPosition }),
        });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || "Labeled sample save failed.");
        }
        renderCaptures(payload.captures || []);
        renderDataset(payload.dataset || {});
        const labels = Object.keys(payload.pieces || {}).length;
        const crops = (payload.manifest && payload.manifest.crops || []).length;
        message.textContent = `Saved labeled sample with ${labels} labels and ${crops} analyzed crops.`;
      } catch (error) {
        message.textContent = error.message;
      } finally {
        control.disabled = false;
        refreshReadiness();
      }
    }

    async function exportSquareCrops(filename, control) {
      control.disabled = true;
      try {
        const response = await fetch(`/captures/${encodeURIComponent(filename)}/export-squares`, { method: "POST" });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || "Square crop export failed.");
        }
        renderCaptures(payload.captures || []);
        refreshDataset();
        const count = (payload.manifest && payload.manifest.crops || []).length;
        message.textContent = `Exported ${count} square crops.`;
      } catch (error) {
        message.textContent = error.message;
      } finally {
        control.disabled = false;
        refreshReadiness();
      }
    }

    async function detectBoard(filename, control) {
      control.disabled = true;
      try {
        const response = await fetch(`/captures/${encodeURIComponent(filename)}/detect-board`, { method: "POST" });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || "Board detection failed.");
        }
        applyCalibration(payload.calibration);
        renderCaptures(payload.captures || []);
        refreshDataset();
        const count = (payload.manifest && payload.manifest.crops || []).length;
        const error = payload.detection ? payload.detection.reprojection_error_px : "-";
        message.textContent = `Board detected; ${count} square crops refreshed (error ${error}px).`;
      } catch (error) {
        message.textContent = error.message;
      } finally {
        control.disabled = false;
        refreshReadiness();
      }
    }

    async function analyzeSquareCrops(filename, control) {
      control.disabled = true;
      try {
        const response = await fetch(`/captures/${encodeURIComponent(filename)}/analyze-squares`, { method: "POST" });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || "Square analysis failed.");
        }
        renderCaptures(payload.captures || []);
        refreshDataset();
        const count = (payload.manifest && payload.manifest.crops || []).length;
        message.textContent = `Analyzed ${count} square crops.`;
      } catch (error) {
        message.textContent = error.message;
      } finally {
        control.disabled = false;
        refreshReadiness();
      }
    }

    async function predictOccupancy(filename, control) {
      control.disabled = true;
      try {
        const response = await fetch(`/captures/${encodeURIComponent(filename)}/predict-occupancy`, { method: "POST" });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || "Occupancy prediction failed.");
        }
        renderCaptures(payload.captures || []);
        const predictions = (payload.manifest && payload.manifest.predictions || []);
        const occupied = predictions.filter((prediction) => prediction.occupied).length;
        message.textContent = `Predicted ${occupied} occupied squares.`;
      } catch (error) {
        message.textContent = error.message;
      } finally {
        control.disabled = false;
        refreshReadiness();
      }
    }

    function applyPredictedPosition(payload) {
      loadBoardPosition(payload.predicted_pieces || {}, payload.predicted_fen, currentCalibration(), null);
      return Object.keys(boardPosition).length;
    }

    async function predictPieces(filename, control) {
      control.disabled = true;
      try {
        const response = await fetch(`/captures/${encodeURIComponent(filename)}/predict-pieces`, { method: "POST" });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || "Piece prediction failed.");
        }
        renderCaptures(payload.captures || []);
        const count = applyPredictedPosition(payload);
        message.textContent = `Predicted ${count} pieces; overlay updated.`;
      } catch (error) {
        message.textContent = error.message;
      } finally {
        control.disabled = false;
        refreshReadiness();
      }
    }

    async function recognizePosition(filename, control) {
      control.disabled = true;
      try {
        const response = await fetch(`/captures/${encodeURIComponent(filename)}/recognize-position`, { method: "POST" });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || "Position recognition failed.");
        }
        renderCaptures(payload.captures || []);
        const count = applyPredictedPosition(payload);
        const summary = payload.recognition_summary || {};
        const confidence = summary.average_confidence ?? "-";
        message.textContent = `Recognized ${count} pieces; avg confidence ${confidence}.`;
      } catch (error) {
        message.textContent = error.message;
      } finally {
        control.disabled = false;
        refreshReadiness();
      }
    }

    async function detectLiveBoard() {
      detectLiveButton.disabled = true;
      message.textContent = "Saving current frame and detecting board...";
      try {
        const response = await fetch("/snapshot/detect-board", { method: "POST" });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || "Live board detection failed.");
        }
        applyCalibration(payload.calibration);
        renderCaptures(payload.captures || []);
        refreshDataset();
        const count = (payload.manifest && payload.manifest.crops || []).length;
        const error = payload.detection ? payload.detection.reprojection_error_px : "-";
        message.textContent = `Saved ${payload.filename}; detected board with ${count} crops (error ${error}px).`;
      } catch (error) {
        message.textContent = error.message;
      } finally {
        detectLiveButton.disabled = false;
        refreshHealth();
        refreshReadiness();
      }
    }

    async function recognizeLivePosition() {
      recognizeLiveButton.disabled = true;
      message.textContent = "Saving and recognizing current frame...";
      try {
        const response = await fetch("/snapshot/recognize-position", { method: "POST" });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || "Live recognition failed.");
        }
        renderCaptures(payload.captures || []);
        const count = applyPredictedPosition(payload);
        const summary = payload.recognition_summary || {};
        const confidence = summary.average_confidence ?? "-";
        message.textContent = `Saved ${payload.filename}; recognized ${count} pieces (${confidence} avg confidence).`;
      } catch (error) {
        message.textContent = error.message;
      } finally {
        recognizeLiveButton.disabled = false;
        refreshHealth();
        refreshReadiness();
      }
    }

    function showLastCapture(payload) {
      if (!payload || !payload.filename) {
        return;
      }
      const url = `/captures/${encodeURIComponent(payload.filename)}?ts=${Date.now()}`;
      lastCaptureImage.src = url;
      lastCaptureLink.href = url;
      lastCaptureLink.textContent = payload.filename;
      lastCapturePanel.hidden = false;
    }

    async function saveSnapshot(endpoint, control, progressText, errorText, options = {}) {
      control.disabled = true;
      message.textContent = progressText;
      try {
        if (options.applyFirst) {
          message.textContent = "Applying settings before capture...";
          await applyCameraSettings({ silent: true });
          await wait(options.settleMs || 650);
          message.textContent = progressText;
        }
        const response = await fetch(endpoint, { method: "POST" });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || errorText);
        }
        message.textContent = `Saved ${payload.filename}`;
        showLastCapture(payload);
        renderCaptures(payload.captures || []);
      } catch (error) {
        message.textContent = error.message;
      } finally {
        control.disabled = false;
        refreshHealth();
        refreshReadiness();
      }
    }

    button.addEventListener("click", () => {
      saveSnapshot("/snapshot", button, "Saving snapshot...", "Snapshot failed.", { applyFirst: true });
    });

    hdrButton.addEventListener("click", () => {
      saveSnapshot("/snapshot/hdr", hdrButton, "Capturing HDR stack...", "HDR snapshot failed.", { applyFirst: true, settleMs: 850 });
    });

    function isTypingTarget(target) {
      if (!target) {
        return false;
      }
      if (target.isContentEditable) {
        return true;
      }
      const tagName = target.tagName;
      if (tagName === "TEXTAREA") {
        return true;
      }
      if (tagName !== "INPUT") {
        return false;
      }
      return ["text", "number", "search", "email", "password", "tel", "url"].includes((target.type || "").toLowerCase());
    }

    document.addEventListener("keydown", (event) => {
      if (event.defaultPrevented || event.repeat || event.key.toLowerCase() !== "p" || isTypingTarget(event.target)) {
        return;
      }
      event.preventDefault();
      if (!button.disabled) {
        saveSnapshot("/snapshot", button, "Saving snapshot...", "Snapshot failed.", { applyFirst: true });
      }
    });

    gridToggle.addEventListener("change", () => {
      boardOverlay.classList.toggle("hidden", !gridToggle.checked);
    });

    detectLiveButton.addEventListener("click", detectLiveBoard);
    recognizeLiveButton.addEventListener("click", recognizeLivePosition);
    diagnosticsButton.addEventListener("click", refreshDiagnostics);
    refreshReadinessButton.addEventListener("click", refreshReadiness);
    saveCalibrationButton.addEventListener("click", saveCalibration);
    savePositionButton.addEventListener("click", () => savePosition());
    startPositionButton.addEventListener("click", () => usePositionPreset("/position/start", "Starting position loaded."));
    clearPositionButton.addEventListener("click", () => usePositionPreset("/position/clear", "Board cleared."));
    refreshCapturesButton.addEventListener("click", refreshCaptures);
    deleteAllCapturesButton.addEventListener("click", deleteAllCaptures);
    refreshDatasetButton.addEventListener("click", refreshDataset);
    trainOccupancyButton.addEventListener("click", trainOccupancyModel);
    trainPieceButton.addEventListener("click", trainPieceModel);
    evaluatePieceButton.addEventListener("click", evaluatePieceModel);
    applyCameraSettingsButton.addEventListener("click", () => {
      applyCameraSettings().catch(() => {});
    });
    autofocusButton.addEventListener("click", triggerAutofocus);
    cameraModeSelect.addEventListener("change", () => applyCameraSettings().catch(() => {}));
    colorOrderSelect.addEventListener("change", () => applyCameraSettings().catch(() => {}));
    exposureModeSelect.addEventListener("change", updateTuningOutputs);
    [
      exposureValueSlider,
      exposureTimeSlider,
      analogueGainSlider,
      brightnessSlider,
      contrastSlider,
      saturationSlider,
      sharpnessSlider,
      shadowLiftSlider,
      purpleFixSlider,
    ].forEach((slider) => {
      slider.addEventListener("input", updateTuningOutputs);
    });
    focusModeSelect.addEventListener("change", () => {
      lensPositionSlider.disabled = focusModeSelect.value !== "manual";
      focusLabel.textContent = focusModeSelect.value;
      applyFocusSettings({ trigger: focusModeSelect.value === "auto" });
    });
    afRangeSelect.addEventListener("change", () => applyFocusSettings());
    afSpeedSelect.addEventListener("change", () => applyFocusSettings());
    lensPositionSlider.addEventListener("input", () => {
      lensPositionValue.textContent = Number(lensPositionSlider.value).toFixed(1);
      scheduleManualFocusApply();
    });
    [leftSlider, topSlider, sizeSlider, orientationToggle].forEach((slider) => {
      slider.addEventListener("input", () => applyCalibration(currentCalibration()));
    });

    refreshCameraSettings();
    refreshHealth();
    refreshDiagnostics();
    refreshCalibration();
    refreshPosition();
    refreshCaptures();
    refreshDataset();
    refreshOccupancyModel();
    refreshPieceModel();
    refreshReadiness();
    setInterval(refreshHealth, 3000);
    setInterval(refreshReadiness, 6000);
  </script>
</body>
</html>
"""


@app.get("/")
def index():
    return render_template_string(INDEX_HTML)


@app.get("/stream.mjpg")
def stream():
    return Response(
        mjpeg_frames(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.post("/snapshot")
def snapshot():
    frame = camera_manager.capture_rgb()
    if frame is None:
        return jsonify({"ok": False, "error": "Camera is not available yet."}), 503

    capture = save_frame_capture(frame)
    return jsonify({"ok": True, **capture, "captures": capture_entries(limit=10)})


@app.post("/snapshot/hdr")
def snapshot_hdr():
    result = camera_manager.capture_hdr_rgb()
    if result is None:
        return jsonify({"ok": False, "error": "Camera is not available yet."}), 503

    capture = save_frame_capture(
        result["frame"],
        extra_metadata=result.get("metadata", {}),
        filename_prefix="hdr",
    )
    return jsonify({"ok": True, **capture, "captures": capture_entries(limit=10)})


@app.post("/snapshot/detect-board")
def snapshot_detect_board():
    frame = camera_manager.capture_rgb()
    if frame is None:
        return jsonify({"ok": False, "error": "Camera is not available yet."}), 503

    capture = save_frame_capture(frame)
    try:
        result = detect_board_from_capture(capture["filename"])
    except (RuntimeError, TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc), **capture}), 400
    return jsonify({"ok": True, **capture, **result, "captures": capture_entries(limit=10)})


@app.post("/snapshot/recognize-position")
def snapshot_recognize_position():
    frame = camera_manager.capture_rgb()
    if frame is None:
        return jsonify({"ok": False, "error": "Camera is not available yet."}), 503
    if load_piece_model() is None:
        return jsonify({"ok": False, "error": "Train the piece model before recognition."}), 400

    capture = save_frame_capture(frame)
    try:
        manifest = recognize_capture_position(capture["filename"])
    except (RuntimeError, TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc), **capture}), 400
    return jsonify(
        {
            "ok": True,
            **capture,
            **recognition_payload(manifest),
            "captures": capture_entries(limit=10),
        }
    )


@app.get("/health")
def health():
    return jsonify(camera_manager.status())


@app.get("/camera-settings")
def get_camera_settings():
    status = camera_manager.status()
    return jsonify({"ok": True, **status})


@app.post("/camera-settings")
def post_camera_settings():
    try:
        settings = camera_manager.update_settings(request.get_json(force=True, silent=False))
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    status = camera_manager.status()
    return jsonify({"ok": True, "settings": settings, "health": status})


@app.post("/focus")
def post_focus():
    try:
        result = camera_manager.update_focus(request.get_json(force=True, silent=False))
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    status_code = 200 if result.get("ok") else 400
    return jsonify(result), status_code


@app.post("/focus/trigger")
def trigger_focus():
    result = camera_manager.trigger_autofocus()
    status_code = 200 if result.get("ok") else 400
    return jsonify(result), status_code


@app.get("/diagnostics")
def diagnostics():
    return jsonify(camera_diagnostics())


@app.get("/readiness")
def readiness():
    return jsonify(readiness_payload())


@app.get("/captures")
def captures_manifest():
    return jsonify({"ok": True, "captures": capture_entries()})


@app.delete("/captures")
def delete_all_captures_route():
    result = delete_all_capture_artifacts()
    return jsonify({"ok": True, **result, "captures": capture_entries(), "dataset": dataset_manifest()})


@app.get("/dataset")
def dataset():
    return jsonify(dataset_manifest())


@app.get("/dataset.csv")
def dataset_csv():
    return Response(
        dataset_csv_text(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=chessv2-square-dataset.csv"},
    )


@app.get("/model/occupancy")
def get_occupancy_model():
    return jsonify(occupancy_model_payload())


@app.post("/model/occupancy/train")
def train_occupancy():
    try:
        model = train_occupancy_model()
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "available": True, "model": model})


@app.get("/model/piece")
def get_piece_model():
    return jsonify(piece_model_payload())


@app.post("/model/piece/train")
def train_piece():
    try:
        model = train_piece_model()
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "available": True, "model": model})


@app.get("/model/piece/evaluate")
def evaluate_piece():
    try:
        evaluation = evaluate_piece_model()
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify(evaluation)


@app.get("/captures/<path:filename>")
def capture_file(filename: str):
    allowed_suffixes = {".jpg", ".json"}
    path = CAPTURE_DIR / filename
    if path.suffix.lower() not in allowed_suffixes:
        return jsonify({"ok": False, "error": "Unsupported capture file type."}), 404
    return send_from_directory(CAPTURE_DIR, filename)


@app.delete("/captures/<path:filename>")
def delete_capture_route(filename: str):
    try:
        result = delete_capture_artifacts(filename)
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "Capture image not found."}), 404
    except (RuntimeError, TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, **result, "captures": capture_entries(), "dataset": dataset_manifest()})


@app.post("/captures/<path:filename>/detect-board")
def detect_capture_board(filename: str):
    try:
        result = detect_board_from_capture(filename)
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "Capture image not found."}), 404
    except (RuntimeError, TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, **result, "captures": capture_entries()})


@app.post("/captures/<path:filename>/export-squares")
def export_capture_squares(filename: str):
    try:
        manifest = export_square_crops(filename)
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "Capture image not found."}), 404
    except (RuntimeError, TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "manifest": manifest, "captures": capture_entries()})


@app.post("/captures/<path:filename>/labels")
def label_capture(filename: str):
    try:
        result = update_capture_labels(filename, request.get_json(force=True, silent=False))
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "Capture image not found."}), 404
    except (RuntimeError, TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, **result, "captures": capture_entries()})


@app.post("/captures/<path:filename>/save-labeled-sample")
def save_capture_labeled_sample(filename: str):
    try:
        result = save_labeled_sample(filename, request.get_json(force=True, silent=False))
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "Capture image not found."}), 404
    except (RuntimeError, TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, **result, "captures": capture_entries(), "dataset": dataset_manifest()})


@app.post("/captures/<path:filename>/analyze-squares")
def analyze_capture_squares(filename: str):
    try:
        manifest = analyze_square_crops(filename)
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "Capture image not found."}), 404
    except (RuntimeError, TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "manifest": manifest, "captures": capture_entries()})


@app.post("/captures/<path:filename>/predict-occupancy")
def predict_capture_occupancy_route(filename: str):
    try:
        manifest = predict_capture_occupancy(filename)
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "Capture image not found."}), 404
    except (RuntimeError, TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "manifest": manifest, "captures": capture_entries()})


@app.post("/captures/<path:filename>/predict-pieces")
def predict_capture_pieces_route(filename: str):
    try:
        manifest = predict_capture_pieces(filename)
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "Capture image not found."}), 404
    except (RuntimeError, TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, **recognition_payload(manifest), "captures": capture_entries()})


@app.post("/captures/<path:filename>/recognize-position")
def recognize_capture_position_route(filename: str):
    try:
        manifest = recognize_capture_position(filename)
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "Capture image not found."}), 404
    except (RuntimeError, TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, **recognition_payload(manifest), "captures": capture_entries()})


@app.get("/calibration")
def get_calibration():
    return jsonify({"ok": True, "calibration": load_calibration()})


@app.post("/calibration")
def post_calibration():
    try:
        calibration = save_calibration(request.get_json(force=True, silent=False))
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "calibration": calibration})


@app.get("/board-map")
def board_map():
    calibration = load_calibration()
    width, height = active_frame_size()
    return jsonify(
        {
            "ok": True,
            "calibration": calibration,
            "resolution": [width, height],
            "squares": board_squares(calibration, (width, height)),
        }
    )


@app.get("/position")
def get_position():
    return jsonify(position_payload())


@app.post("/position")
def post_position():
    try:
        position = save_position(request.get_json(force=True, silent=False))
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify(position_payload(position))


@app.post("/position/clear")
def clear_position():
    position = save_position({"pieces": {}})
    return jsonify(position_payload(position))


@app.post("/position/start")
def start_position():
    position = save_position({"pieces": starting_position()})
    return jsonify(position_payload(position))


if __name__ == "__main__":
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    app.run(host=HOST, port=PORT, threaded=True)
