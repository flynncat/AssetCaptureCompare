import datetime
import hashlib
import json
import os
import time
import uuid
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[3]
VIEWS = [
    {"id": "front", "label": "Front", "yaw": 0, "elevation": 0},
    {"id": "front_right", "label": "Front 45", "yaw": 45, "elevation": 0},
    {"id": "right", "label": "Right", "yaw": 90, "elevation": 0},
    {"id": "back", "label": "Back", "yaw": 180, "elevation": 0},
    {"id": "left", "label": "Left", "yaw": 270, "elevation": 0},
    {"id": "back_right", "label": "Back 45", "yaw": 135, "elevation": 0},
    {"id": "top", "label": "Top", "yaw": 0, "elevation": 75},
    {"id": "bottom", "label": "Bottom", "yaw": 0, "elevation": -75},
]
DEFAULT_PROFILE = {
    "schema": 1, "size": 480, "columns": 4, "fov": 35.0, "margin": 0.05,
    "padding": 0.0, "views": VIEWS, "warmup_frames": 0,
    "scene": "/AssetCaptureCompare/Maps/CaptureDefault", "hide_floor": True,
    "lod": 0, "exposure": 1.0, "timeout": 240, "orientation": 0.0,
    "renderer": "final_color_ldr_fxaa_resident_snapshot_v5", "reframe": False, "worker_memory_gib": 24,
}


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def uid():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:12]


def read_json(path, default=None):
    for attempt in range(8):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(0.01 * (attempt + 1))


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        for attempt in range(6):
            try:
                os.replace(str(temporary), str(path))
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        if temporary.exists():
            temporary.unlink()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def file_hash(path):
    hasher = hashlib.sha256()
    with open(str(path), "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def runtime_revision():
    directory = PLUGIN / "Content/Python"
    return digest({p.relative_to(directory).as_posix(): file_hash(p) for p in sorted(directory.rglob("*.py"))})


def file_stamps(paths):
    result = {}
    for name in paths:
        try:
            stat = Path(name).stat()
            result[name] = [stat.st_size, stat.st_mtime_ns]
        except FileNotFoundError:
            result[name] = None
    return result


def validate_profile(profile):
    if not 32 <= int(profile["size"]) <= 2000:
        raise ValueError("View size must be between 32 and 2000")
    if not 1 <= int(profile["columns"]) <= 16 or int(profile["size"]) * int(profile["columns"]) > 2000:
        raise ValueError("Contact sheet width must not exceed 2000 pixels")
    if not 5 <= float(profile["fov"]) <= 120 or not 0.01 <= float(profile["margin"]) <= 0.25:
        raise ValueError("Invalid field of view or margin")
    views = profile["views"]
    if not views or len(views) > 32 or len({v["id"] for v in views}) != len(views):
        raise ValueError("Expected 1-32 unique view IDs")
    for view in views:
        if not re_safe_id(view["id"]) or not -89.9 <= float(view["elevation"]) <= 89.9:
            raise ValueError("Invalid view")
    if not 0 <= int(profile["warmup_frames"]) <= 240 or not 30 <= int(profile["timeout"]) <= 3600:
        raise ValueError("Invalid warmup or timeout")
    if float(profile["padding"]) < 0:
        raise ValueError("Bounds padding must be non-negative")
    if not 4 <= float(profile["worker_memory_gib"]) <= 128:
        raise ValueError("Worker memory budget must be between 4 and 128 GiB")
    # Browser JSON encodes 35.0 as 35; equivalent settings must hash identically.
    for key in ("fov", "margin", "padding", "exposure", "orientation"):
        profile[key] = float(profile[key])
    for key in ("size", "columns", "warmup_frames", "timeout", "lod", "worker_memory_gib"):
        profile[key] = int(profile[key])
    return profile


def re_safe_id(value):
    import re
    return bool(re.fullmatch(r"[A-Za-z0-9_-]+", str(value)))


def contained(root, relative):
    root = Path(root).resolve()
    target = (root / relative).resolve()
    if target == root or root not in target.parents:
        raise ValueError("Path is outside the output directory")
    return target
