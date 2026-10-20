import math
import threading
from pathlib import Path
import numpy as np
from PIL import Image
import skimage
from skimage.metrics import structural_similarity
from .common import contained, digest, file_hash, read_json, utcnow, write_json

PARAMETERS = {"schema": 2, "library": "scikit-image", "version": skimage.__version__, "data_range": 1.0,
              "gaussian_weights": True, "sigma": 1.5, "use_sample_covariance": False, "K1": 0.01, "K2": 0.03}


def similarity(left, right, full=False):
    options = {k: v for k, v in PARAMETERS.items() if k not in ("schema", "library", "version")}
    scores, maps = [], []
    for channel in range(3):
        result = structural_similarity(left[:, :, channel], right[:, :, channel], full=full, **options)
        if full:
            scores.append(float(result[0]))
            maps.append(result[1])
        else:
            scores.append(float(result))
    return (float(np.mean(scores)), np.mean(maps, axis=0)) if full else float(np.mean(scores))


def heatmap(values):
    strength = np.clip((1 - values) / 0.15, 0, 1)
    return np.stack((strength, strength * (1 - strength), np.zeros_like(strength)), axis=-1)


def contact_sheet(directory, capture, profile, cancel_event=None):
    directory = Path(directory)
    size, columns = int(profile["size"]), int(profile["columns"])
    sheet = Image.new("RGB", (size * columns, size * math.ceil(len(capture["views"]) / columns)), "black")
    for index, view in enumerate(capture["views"]):
        if cancel_event and cancel_event.is_set():
            raise InterruptedError("Image processing cancelled")
        path = contained(directory, view["file"])
        with Image.open(path) as image:
            image.load()
            if image.size != (size, size) or image.format != "PNG":
                raise ValueError("Invalid capture image: " + str(path))
            rgb = image.convert("RGB")
            sheet.paste(rgb, ((index % columns) * size, (index // columns) * size))
            normalize = image.mode != "RGB"
        if normalize:
            rgb.save(path, compress_level=1)
        view["sha256"] = file_hash(path)
    sheet.save(directory / "contact_sheet.png", compress_level=1)
    capture["sheet_sha256"] = file_hash(directory / "contact_sheet.png")


class Comparer:
    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()

    def ensure_sheets(self, directory, result, capture, cancel_event=None):
        if result["status"] not in ("success", "incomplete"):
            return
        paths = [directory / ("sheet_" + mode + ".png") for mode in ("diff", "heat")]
        if all(path.is_file() for path in paths):
            return
        with Image.open(self.store.capture_path(capture["capture_id"]) / "contact_sheet.png") as image:
            sheet_size = image.size
        with Image.open(directory / (result["views"][0]["id"] + "_diff.png")) as image:
            width, height = image.size
        columns = max(1, sheet_size[0] // width)
        for mode, path in zip(("diff", "heat"), paths):
            if path.is_file():
                continue
            sheet = Image.new("RGB", sheet_size, "black")
            for index, view in enumerate(result["views"]):
                if cancel_event and cancel_event.is_set():
                    raise InterruptedError("Comparison cancelled")
                with Image.open(directory / (view["id"] + "_" + mode + ".png")) as image:
                    sheet.paste(image, ((index % columns) * width, (index // columns) * height))
            temporary = path.with_suffix(".tmp")
            try:
                sheet.save(temporary, format="PNG", compress_level=1)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)

    def compare(self, left_id, right_id, thresholds=None, cancel_event=None, include_sheets=False):
        thresholds = thresholds or [0.99, 0.97]
        if (len(thresholds) != 2 or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                and math.isfinite(v) and -1 <= v <= 1 for v in thresholds) or thresholds[0] < thresholds[1]):
            raise ValueError("Expected two ordered SSIM thresholds between -1 and 1")
        left, right = self.store.capture(left_id), self.store.capture(right_id)
        if not self.store.verify(left) or not self.store.verify(right):
            raise ValueError("Capture is incomplete, corrupt or missing")
        key = digest({"left": left_id, "right": right_id, "files": [left["views"], right["views"]],
                      "parameters": PARAMETERS, "thresholds": thresholds})
        with self.lock:
            directory = contained(self.store.root, "comparisons/" + key)
            cached = read_json(directory / "comparison.json")
            if cached:
                if include_sheets:
                    self.ensure_sheets(directory, cached, left, cancel_event)
                return cached
            result = {"comparison_id": key, "created_at": utcnow(), "left": left_id, "right": right_id,
                      "parameters": PARAMETERS, "thresholds": thresholds,
                      "manual_pair": left["identity"] != right["identity"], "views": []}
            reasons = [k for k in ("compatibility", "framing_id") if left.get(k) != right.get(k)]
            if [v["id"] for v in left["views"]] != [v["id"] for v in right["views"]]:
                reasons.append("view_ids")
            if reasons:
                result.update(status="incompatible", reasons=reasons)
                a, b = left.get("compatibility_details", {}), right.get("compatibility_details", {})
                result["changed_settings"] = [key for key in sorted(set(a) | set(b)) if a.get(key) != b.get(key)]
                if a.get("profile") and b.get("profile"):
                    result["changed_profile_fields"] = [key for key in sorted(set(a["profile"]) | set(b["profile"]))
                        if a["profile"].get(key) != b["profile"].get(key)]
            else:
                for a, b in zip(left["views"], right["views"]):
                    if cancel_event and cancel_event.is_set():
                        raise InterruptedError("Comparison cancelled")
                    with Image.open(contained(self.store.capture_path(left_id), a["file"])) as image:
                        x = np.asarray(image.convert("RGB"), dtype=np.float64) / 255.0
                    with Image.open(contained(self.store.capture_path(right_id), b["file"])) as image:
                        y = np.asarray(image.convert("RGB"), dtype=np.float64) / 255.0
                    if x.shape != y.shape:
                        raise ValueError("Image dimensions differ")
                    h, w = x.shape[:2]
                    roi = [max(0, math.floor(min(a["roi"][0], b["roi"][0]))),
                           max(0, math.floor(min(a["roi"][1], b["roi"][1]))),
                           min(w, math.ceil(max(a["roi"][2], b["roi"][2]))),
                           min(h, math.ceil(max(a["roi"][3], b["roi"][3])))]
                    valid_roi = roi[2] - roi[0] >= 11 and roi[3] - roi[1] >= 11
                    if np.array_equal(x, y):
                        global_score, ssim_map = 1.0, np.ones((h, w), dtype=np.float64)
                        roi_score = 1.0 if valid_roi else None
                    else:
                        global_score, ssim_map = similarity(x, y, full=True)
                        roi_score = similarity(x[roi[1]:roi[3], roi[0]:roi[2]], y[roi[1]:roi[3], roi[0]:roi[2]]) if valid_roi else None
                    directory.mkdir(parents=True, exist_ok=True)
                    Image.fromarray(np.round(np.abs(x - y) * 255).astype(np.uint8)).save(directory / (a["id"] + "_diff.png"), compress_level=1)
                    Image.fromarray(np.round(heatmap(ssim_map) * 255).astype(np.uint8)).save(directory / (a["id"] + "_heat.png"), compress_level=1)
                    result["views"].append({"id": a["id"], "roi": roi, "score": roi_score, "full_score": global_score,
                        "status": "success" if valid_roi else "invalid_roi",
                        "reason": None if valid_roi else "Projected ROI is smaller than the SSIM window"})
                invalid = [v["id"] for v in result["views"] if v["score"] is None]
                if invalid:
                    result.update(status="incomplete", reasons=["invalid_roi"], invalid_views=invalid)
                else:
                    worst = min(result["views"], key=lambda v: v["score"])
                    result.update(status="success", score=worst["score"], worst_view=worst["id"],
                                  mean=float(np.mean([v["score"] for v in result["views"]])),
                                  category="small" if worst["score"] >= thresholds[0] else "review" if worst["score"] >= thresholds[1] else "changed")
            write_json(directory / "comparison.json", result)
            if include_sheets:
                self.ensure_sheets(directory, result, left, cancel_event)
            return result
