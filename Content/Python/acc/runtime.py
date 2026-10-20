"""Locate the portable runtime without consulting machine Python installations."""
import json
from pathlib import Path


def local_python(plugin):
    plugin = Path(plugin).resolve()
    runtime = plugin / ".runtime"
    executable = runtime / "python.exe"
    lock = json.loads((plugin / "Scripts/runtime-lock.json").read_text(encoding="utf-8"))
    try:
        manifest = json.loads((runtime / "runtime-lock.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = None
    if (not executable.is_file() or plugin not in executable.resolve().parents or
            (runtime / "pyvenv.cfg").exists() or manifest != lock):
        raise RuntimeError("Install the plugin-local portable runtime: " + str(plugin / "Scripts/setup_runtime.ps1"))
    return executable
