"""Check interpreter, import provenance, pinned versions and native image operations."""
import importlib
import importlib.metadata
import json
import sys
from pathlib import Path


def verify():
    plugin = Path(__file__).resolve().parents[1]
    runtime = Path(sys.executable).resolve().parent
    lock = json.loads((plugin / "Scripts/runtime-lock.json").read_text(encoding="utf-8"))
    assert runtime.parent == plugin, "Interpreter must live inside this plugin"
    assert sys.prefix == sys.base_prefix, "A venv is not a portable runtime"
    assert sys.flags.isolated and sys.flags.ignore_environment, "Embedded isolation must be enabled"
    assert sys.version.split()[0] == lock["python"]["version"], "Unexpected Python version"
    assert json.loads((runtime / "runtime-lock.json").read_text()) == lock, "Runtime lock mismatch"
    for entry in sys.path:
        path = Path(entry).resolve()
        assert path == plugin or plugin in path.parents, "External sys.path entry: " + str(path)
    for item in lock["packages"]:
        assert importlib.metadata.version(item["name"]) == item["version"], item["name"]
    for name in ("numpy", "scipy", "skimage", "PIL", "psutil", "networkx", "imageio", "tifffile", "packaging", "lazy_loader", "acc"):
        module = importlib.import_module(name)
        assert plugin in Path(module.__file__).resolve().parents, name + " imported from outside plugin"
    import numpy as np
    from PIL import Image
    from skimage.metrics import structural_similarity
    from scipy.ndimage import gaussian_filter
    pixels = np.array(Image.new("RGB", (16, 16), (30, 90, 120)))
    assert structural_similarity(pixels, pixels, channel_axis=2, data_range=255) == 1.0
    assert gaussian_filter(pixels, 1).shape == pixels.shape
    print("Portable runtime verified: Python " + lock["python"]["version"] + ", all imports inside plugin")


if __name__ == "__main__":
    verify()
