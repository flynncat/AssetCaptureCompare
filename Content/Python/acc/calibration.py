import copy
import statistics
import threading
import time
from .common import representative_assets, uid, utcnow, write_json


def repeatability(store, asset):
    comparison = asset.get("comparison", {})
    try:
        left = store.capture(asset["baseline_id"])
        right = store.capture(asset["capture_id"])
        if (left.get("compatibility") != right.get("compatibility") or
                left.get("framing_id") != right.get("framing_id") or
                not store.verify(left) or not store.verify(right)):
            return False, False
        if comparison.get("status") == "success":
            return comparison.get("score", -1) >= 0.995, False
        exact = (bool(left["views"]) and
                 [(v["id"], v["sha256"]) for v in left["views"]] ==
                 [(v["id"], v["sha256"]) for v in right["views"]])
        return bool(exact), bool(exact)
    except (KeyError, OSError, ValueError, TypeError):
        return False, False


class Calibration:
    def __init__(self, scheduler, on_recommended=None):
        self.scheduler = scheduler
        self.thread = None
        self.cancelled = threading.Event()
        self.phase = ""
        self.result = None
        self.on_recommended = on_recommended

    @property
    def running(self):
        return bool(self.thread and self.thread.is_alive())

    def start(self, prepared, profile, maximum, cancel_event=None):
        if self.running or self.scheduler.running:
            raise ValueError("Wait for the current Capture or calibration")
        if len(prepared["assets"]) < 10:
            raise ValueError("Calibration needs at least 10 representative assets; use 50 for acceptance")
        if not isinstance(maximum, int) or not 2 <= maximum <= 8:
            raise ValueError("Maximum calibration concurrency must be between 2 and 8")
        self.cancelled.clear()
        if cancel_event and cancel_event.is_set():
            raise InterruptedError("Calibration cancelled")
        prepared = dict(prepared, assets=representative_assets(prepared["assets"]))
        self.result = None
        self.thread = threading.Thread(target=self.run, args=(prepared, copy.deepcopy(profile), maximum), daemon=True)
        self.thread.start()

    def cancel(self):
        self.cancelled.set()
        self.scheduler.cancel()

    def run(self, prepared, profile, maximum):
        result = {"calibration_id": uid(), "created_at": utcnow(), "status": "running", "results": [], "recommended": None,
                  "asset_paths": [a["path"] for a in prepared["assets"]], "profile": profile,
                  "acceptance_sample_size": len(prepared["assets"]) >= 50,
                  "selection_count": prepared.get("selection_count", len(prepared["assets"])), "sample_count": len(prepared["assets"])}
        try:
            self.phase = "预热资源"
            warmup = self.scheduler.start(prepared, profile, 1, "Calibration resource preparation", cancel_event=self.cancelled)
            self.scheduler.thread.join()
            if self.cancelled.is_set():
                raise InterruptedError("Calibration cancelled")
            if self.scheduler.store.batch(warmup["batch_id"])["status"] != "success":
                raise ValueError("Warmup failed; inspect its batch before calibration")
            self.result = result
            for count in range(1, maximum + 1):
                measurements, batch_ids, metrics, exact_image_checks = [], [], [], []
                stable = True
                for repeat in range(3):
                    if self.cancelled.is_set():
                        raise InterruptedError("Calibration cancelled")
                    self.phase = "{} 并发，第 {}/3 轮".format(count, repeat + 1)
                    try:
                        batch = self.scheduler.start(prepared, profile, count, "Calibration {} workers / {}".format(count, repeat + 1), cancel_event=self.cancelled)
                    except ValueError as error:
                        result["resource_limit"] = str(error)
                        stable = False
                        break
                    self.scheduler.thread.join()
                    batch = self.scheduler.store.batch(batch["batch_id"])
                    if batch["status"] != "success":
                        raise ValueError("Calibration batch failed: " + batch["batch_id"])
                    measurements.append(batch["elapsed_seconds"])
                    batch_ids.append(batch["batch_id"])
                    metrics.append(batch.get("metrics", {}))
                    if batch.get("metrics", {}).get("worker_restarts", 0):
                        stable = False
                    if batch["context"].get("actual_concurrency", count) != count:
                        stable = False
                    if count > 1 and batch.get("metrics", {}).get("peak_busy_workers", 0) < count:
                        stable = False
                    exact_count = 0
                    for asset in batch["assets"]:
                        repeatable, exact = repeatability(self.scheduler.store, asset)
                        exact_count += int(exact)
                        if not repeatable:
                            stable = False
                    exact_image_checks.append(exact_count)
                if len(measurements) < 3:
                    break
                median = statistics.median(measurements)
                baseline = result["results"][0]["median_seconds"] if result["results"] else median
                result["results"].append({"concurrency": count, "median_seconds": median, "seconds": measurements,
                    "batch_ids": batch_ids, "speedup": baseline / median, "stable": stable, "metrics": metrics,
                    "exact_image_checks": exact_image_checks})
                candidates = [r for r in result["results"] if r["stable"]]
                if candidates:
                    fastest = min(r["median_seconds"] for r in candidates)
                    result["recommended"] = min(r["concurrency"] for r in candidates if r["median_seconds"] <= fastest * 1.05)
                self.result = copy.deepcopy(result)
                write_json(self.scheduler.store.root / "calibration.progress.json", result)
                if len(result["results"]) >= 3:
                    previous = result["results"][-2]["median_seconds"]
                    if median >= previous and previous >= result["results"][-3]["median_seconds"]:
                        result["stopped_reason"] = "Two successive concurrency increases did not improve throughput"
                        break
            result["status"] = "success" if result["recommended"] else "unstable"
            result["meets_speedup_target"] = any(r["stable"] and r["speedup"] >= 1.3 for r in result["results"])
            result["machine"] = self.scheduler.store.batch(warmup["batch_id"])["context"]["machine"]
            if result["status"] == "success" and self.on_recommended:
                self.on_recommended(result["recommended"])
        except InterruptedError as error:
            result.update(status="cancelled", error=str(error))
        except Exception as error:
            result.update(status="failed", error=str(error))
        finally:
            self.result = result
            self.phase = result["status"]
            write_json(self.scheduler.store.root / "calibration.json", result)
