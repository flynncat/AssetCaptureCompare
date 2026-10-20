"""Capture a real directory and verify output, optionally interrupting one owned worker."""
import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

import psutil
from PIL import Image

from client import Client

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Content/Python"))
from acc.common import read_json, write_json
from acc.storage import Store


def interrupt_owned_worker(root, batch_id, progress):
    manifest = root / "batches" / batch_id / "workers.json"
    owned_workers = read_json(manifest, [])
    for worker in progress.get("workers", []):
        if not worker.get("path") or worker.get("phase") in ("starting", "idle"):
            continue
        for owned in owned_workers:
            if owned["pid"] != worker["pid"]:
                continue
            try:
                process = psutil.Process(owned["pid"])
                expected = "-accworker=" + owned["job"]
                if (process.name().lower() == "ue4editor.exe" and
                        abs(process.create_time() - owned["created"]) < 1 and
                        any(arg.lower() == expected.lower() for arg in process.cmdline())):
                    process.kill()
                    process.wait(timeout=10)
                    return {"pid": owned["pid"], "job": owned["job"], "asset": worker["path"]}
            except psutil.NoSuchProcess:
                continue
    return None


def run(args):
    client = Client(args.url)
    previous = None
    while args.wait_calibration:
        calibration = client.get("/api/calibration")
        if not calibration["running"]:
            if (calibration.get("result") or {}).get("status") != "success":
                raise RuntimeError("Calibration did not finish successfully")
            break
        if calibration["phase"] != previous:
            previous = calibration["phase"]
            print("Waiting for calibration: " + previous, flush=True)
        time.sleep(5)
    state = client.get("/api/state")
    root = Path(state["root"])
    assets = client.post("/api/editor", {"action": "scan", "path": args.directory})["assets"]
    if not assets:
        raise RuntimeError("No candidate assets")
    paths = [a["path"] for a in assets]
    count = args.workers or state["settings"]["concurrency"]
    started = client.post("/api/capture", {"paths": paths + paths[:1], "concurrency": count,
        "label": "Acceptance full directory / worker recovery" if args.inject_worker_crash else "Acceptance full directory"})
    batch_id = started["batch_id"]
    print("Batch started: " + batch_id + " / " + str(len(paths)) + " assets", flush=True)
    interrupted = None
    previous = None
    while True:
        batch = client.get("/api/batch?id=" + batch_id)
        if batch["status"] != "running":
            break
        progress = client.get("/api/progress")
        if args.inject_worker_crash and interrupted is None and progress.get("batch", {}).get("batch_id") == batch_id:
            interrupted = interrupt_owned_worker(root, batch_id, progress)
            if interrupted:
                print("Interrupted owned worker: " + json.dumps(interrupted), flush=True)
        completed = sum(a["status"] == "success" for a in batch["assets"])
        milestone = completed // 10
        if milestone != previous:
            previous = milestone
            print("Completed: {}/{}".format(completed, len(paths)), flush=True)
        time.sleep(3)
    store = Store(root)
    errors = []
    sizes = Counter()
    cleanup_counts = Counter()
    if len(batch["assets"]) != len(set(paths)):
        errors.append("Candidate count or path deduplication mismatch")
    for asset in batch["assets"]:
        try:
            capture = store.capture(asset["capture_id"])
            if not store.verify(capture):
                raise ValueError("Capture failed or image hash validation failed: " + str(capture.get("error")))
            profile = batch["profile"]
            if [v["id"] for v in capture["views"]] != [v["id"] for v in profile["views"]]:
                raise ValueError("Missing or reordered views")
            if not capture.get("resources_verified"):
                raise ValueError("Resource readiness was not verified")
            cleanup_counts[capture.get("live_capture_worlds_after_cleanup")] += 1
            if capture.get("live_capture_worlds_after_cleanup") != 0:
                raise ValueError("Capture world remained after cleanup")
            directory = store.capture_path(asset["capture_id"])
            for path in [directory / "contact_sheet.png"] + [directory / v["file"] for v in capture["views"]]:
                with Image.open(path) as image:
                    image.load()
                    expected = (profile["size"], profile["size"])
                    if path.name == "contact_sheet.png":
                        rows = (len(profile["views"]) + profile["columns"] - 1) // profile["columns"]
                        expected = (profile["columns"] * profile["size"], rows * profile["size"])
                    if image.size != expected:
                        raise ValueError("Unexpected image dimensions: " + str(image.size))
                    if image.width > 2000:
                        raise ValueError("Output image exceeds 2000px")
                    sizes[str(image.size)] += 1
        except (OSError, ValueError, KeyError) as error:
            errors.append({"path": asset["path"], "error": str(error)})
    if args.inject_worker_crash and (not interrupted or batch.get("metrics", {}).get("worker_restarts", 0) < 1):
        errors.append("Owned worker interruption/recovery was not observed")
    report = {"status": "success" if not errors and batch["status"] == "success" else "failed",
        "batch_id": batch_id, "candidate_count": len(paths), "records": len(batch["assets"]),
        "classes": dict(Counter(a["class"] for a in assets)), "batch_status": batch["status"],
        "elapsed_seconds": batch["elapsed_seconds"], "metrics": batch.get("metrics", {}),
        "interrupted_worker": interrupted, "image_sizes": dict(sizes),
        "live_world_counts": dict(cleanup_counts), "errors": errors}
    write_json(root / args.report, report)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report["status"] == "success" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--directory", required=True)
    parser.add_argument("--workers", type=int)
    parser.add_argument("--wait-calibration", action="store_true")
    parser.add_argument("--inject-worker-crash", action="store_true")
    parser.add_argument("--report", default="acceptance_full_directory.json")
    sys.exit(run(parser.parse_args()))
