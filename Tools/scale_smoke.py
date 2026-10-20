"""Measure large-selection cancellation and real capture cost without changing assets."""
import argparse
import json
import math
import statistics
import threading
import time
from pathlib import Path

from client import Client
from acc.common import read_json, representative_assets, write_json
from acc.compare import similarity
from acc.calibration import repeatability
from acc.storage import Store


def wait_for(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.1)
    raise TimeoutError("Acceptance condition timed out")


def run(args):
    client = Client(args.url)
    state = client.get("/api/state")
    root = Path(state["root"])
    if args.analyze_only:
        validate_report(root, args.report)
        return
    assert not client.get("/api/progress?summary=1")["running"]
    source = client.get("/api/batch?id=" + args.source_batch)
    report = {"source_batch": args.source_batch}
    paths = [a["path"] for a in source["assets"]]
    failure = []

    def prepare():
        try:
            client.post("/api/capture", {"paths": paths, "concurrency": 1, "profile": source["profile"], "label": "Scale cancellation probe"})
        except Exception as error:
            failure.append(str(error))

    thread = threading.Thread(target=prepare)
    thread.start()
    wait_for(lambda: client.get("/api/progress?summary=1").get("preparation"))
    started = time.monotonic()
    client.post("/api/cancel", {})
    thread.join(10)
    assert not thread.is_alive() and failure, "Preparation was not cancelled"
    wait_for(lambda: not client.get("/api/progress?summary=1").get("preparation"), 10)
    report["prepare_cancel_seconds"] = time.monotonic() - started
    write_json(root / args.report, report)
    print("Preparation cancelled: {:.3f}s".format(report["prepare_cancel_seconds"]), flush=True)

    successes = [a for a in source["assets"] if a["status"] == "success"]
    if args.exclude_dependency_stem:
        successes = [a for a in successes if not any(Path(p).stem in args.exclude_dependency_stem for p in
            read_json(root / "batches" / args.source_batch / "assets" / a["asset_id"] / "capture.json").get("source_stamps", {}))]
    sample = representative_assets(successes, args.samples)
    # Cancel after the process is spawned but before its expensive editor startup completes.
    batch_id = client.post("/api/capture", {"paths": [sample[0]["path"]], "concurrency": 1,
        "profile": source["profile"], "label": "Worker startup cancellation probe"})["batch_id"]
    wait_for(lambda: client.get("/api/progress?summary=1").get("workers"), 30)
    started = time.monotonic()
    client.post("/api/cancel", {})
    wait_for(lambda: not client.get("/api/progress?summary=1")["running"], 10)
    report["startup_cancel_seconds"] = time.monotonic() - started
    report["startup_cancel_batch"] = batch_id
    write_json(root / args.report, report)
    print("Startup cancelled: {:.3f}s".format(report["startup_cancel_seconds"]), flush=True)

    report["runs"] = []
    for repeat in range(2):
        started = time.monotonic()
        batch_id = client.post("/api/capture", {"paths": [a["path"] for a in sample], "concurrency": 1,
            "profile": source["profile"], "label": "Scale throughput probe " + str(repeat + 1)})["batch_id"]
        preparation_seconds = time.monotonic() - started
        wait_for(lambda: not client.get("/api/progress?summary=1")["running"], 900)
        batch = client.get("/api/batch?id=" + batch_id)
        assert batch["status"] == "success", [(a["path"], a.get("error")) for a in batch["assets"] if a["status"] != "success"]
        timings = {k: statistics.mean(a["timings"].get(k, 0) for a in batch["assets"])
                   for k in {key for a in batch["assets"] for key in a["timings"]}}
        captures = [read_json(root / "batches" / batch_id / "assets" / a["asset_id"] / "capture.json") for a in batch["assets"]]
        old_captures = [read_json(root / "batches" / args.source_batch / "assets" / a["asset_id"] / "capture.json") for a in sample]
        # PNG compression changes bytes, so compare decoded pixels.
        import numpy as np
        from PIL import Image
        exact = 0
        scores, roi_scores = [], []
        for capture, old in zip(captures, old_captures):
            assert capture["live_capture_worlds_after_cleanup"] == 0
            for a, b in zip(capture["views"], old["views"]):
                def pixels(record, view):
                    with Image.open(root / "batches" / record["capture_id"].split("/")[0] / "assets" /
                                    record["capture_id"].split("/")[1] / view["file"]) as image:
                        return np.asarray(image.convert("RGB"))
                x, y = pixels(capture, a), pixels(old, b)
                same = np.array_equal(x, y)
                exact += int(same)
                scores.append(1.0 if same else similarity(x.astype(float) / 255, y.astype(float) / 255))
                roi = [max(0, math.floor(min(a["roi"][0], b["roi"][0]))), max(0, math.floor(min(a["roi"][1], b["roi"][1]))),
                       min(x.shape[1], math.ceil(max(a["roi"][2], b["roi"][2]))), min(x.shape[0], math.ceil(max(a["roi"][3], b["roi"][3])))]
                if roi[2] - roi[0] >= 11 and roi[3] - roi[1] >= 11:
                    roi_scores.append(1.0 if same else similarity(x[roi[1]:roi[3], roi[0]:roi[2]].astype(float) / 255,
                                                                 y[roi[1]:roi[3], roi[0]:roi[2]].astype(float) / 255))
        entry = {"batch_id": batch_id, "preparation_seconds": preparation_seconds,
                 "elapsed_seconds": batch["elapsed_seconds"], "assets": len(sample), "mean_timings": timings,
                 "mean_capture_seconds": statistics.mean(c["capture_seconds"] for c in captures),
                 "metrics": batch["metrics"], "exact_previous_views": exact,
                 "minimum_previous_ssim": min(scores), "minimum_previous_roi_ssim": min(roi_scores),
                 "total_views": sum(len(c["views"]) for c in captures)}
        report["runs"].append(entry)
        write_json(root / args.report, report)
        print(json.dumps(entry), flush=True)
        assert min(scores + roi_scores) >= .995, "Image stability check failed"
        if repeat:
            store = Store(root)
            assert all(repeatability(store, a)[0] for a in batch["assets"]), "Repeated captures are not stable"
    validate_report(root, args.report)
    print("Scale acceptance report: " + str(root / args.report), flush=True)


def validate_report(root, name):
    report = read_json(root / name)
    store = Store(root)
    assert len(report["runs"]) == 2
    for run in report["runs"]:
        assert min(run["minimum_previous_ssim"], run["minimum_previous_roi_ssim"]) >= .995
    batch = store.batch(report["runs"][-1]["batch_id"])
    checks = [repeatability(store, a) for a in batch["assets"]]
    assert all(stable for stable, _ in checks), "Repeated captures are not stable"
    report.update(status="success", stable_repeated_assets=len(checks), exact_thin_roi_assets=sum(exact for _, exact in checks))
    write_json(root / name, report)
    print(json.dumps({k: report[k] for k in ("status", "stable_repeated_assets", "exact_thin_roi_assets")}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--source-batch", required=True)
    parser.add_argument("--samples", type=int, default=24)
    parser.add_argument("--report", default="acceptance_scale.json")
    parser.add_argument("--exclude-dependency-stem", action="append", default=[])
    parser.add_argument("--analyze-only", action="store_true")
    run(parser.parse_args())
