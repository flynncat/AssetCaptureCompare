"""Compare zero-extra-frame captures with a 16-frame reference on real assets."""
import argparse
import json
import math
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

from client import Client

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Content/Python"))
from acc.common import read_json, write_json
from acc.compare import similarity
from acc.storage import Store
from PIL import Image
import numpy as np


def run(args):
    client = Client(args.url)
    assert not client.get("/api/progress")["running"]
    state = client.get("/api/state")
    root = Path(state["root"])
    source = client.get("/api/batch?id=" + args.source_batch)
    selected = source["assets"][:5]
    for kind, count in (("Blueprint", 2), ("SkeletalMesh", 2), ("StaticMesh", 3)):
        selected += [a for a in source["assets"] if a["class"] == kind and a not in selected][:count]
    paths = [a["path"] for a in selected]
    print("Samples: " + str(dict(Counter(a["class"] for a in selected))), flush=True)
    report_path = root / args.report
    report = read_json(report_path) if args.analyze_only else {
        "source_batch": args.source_batch, "paths": paths, "runs": []}
    for extra in (() if args.analyze_only else (16, 0, 0)):
        profile = dict(state["settings"]["profile"], warmup_frames=extra)
        batch_id = client.post("/api/capture", {"paths": paths, "concurrency": 1, "profile": profile,
            "label": "Performance acceptance / extra frames " + str(extra)})["batch_id"]
        print("Started: " + batch_id + " extra=" + str(extra), flush=True)
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            batch = client.get("/api/batch?id=" + batch_id)
            if batch["status"] != "running":
                break
            time.sleep(2)
        assert batch["status"] == "success", batch
        store = Store(root)
        captures = [store.capture(a["capture_id"]) for a in batch["assets"]]
        for capture in captures:
            assert store.verify(capture)
            assert capture["rendered_frames"] == 8 * (extra + 1)
            assert capture["live_capture_worlds_after_cleanup"] == 0
        timings = {key: statistics.median(c["timings"].get(key, 0) for c in captures)
                   for key in {k for c in captures for k in c["timings"]}}
        log = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in (root / "batches" / batch_id / "logs").glob("*.log"))
        assert "Generating Python API stub" not in log
        assert not any("CachedAssetRegistry" in line and ("DeleteFile" in line or "Error deleting" in line)
                       for line in log.splitlines())
        report["runs"].append({"batch_id": batch_id, "extra_frames": extra, "elapsed_seconds": batch["elapsed_seconds"],
            "capture_seconds": [c["capture_seconds"] for c in captures], "median_timings": timings,
            "metrics": batch["metrics"], "capture_ids": [c["capture_id"] for c in captures]})
        print("Completed: " + batch_id + " seconds=" + str(batch["elapsed_seconds"]), flush=True)
        write_json(report_path, report)
    store = Store(root)
    reference = [store.capture(cid) for cid in report["runs"][0]["capture_ids"]]
    for run in report["runs"][1:]:
        scores, roi_scores, exact = [], [], 0
        for original, cid in zip(reference, run["capture_ids"]):
            capture = store.capture(cid)
            assert original["framing"] == capture["framing"], capture["path"]
            for left, right in zip(original["views"], capture["views"]):
                assert left["id"] == right["id"]
                a = np.asarray(Image.open(store.capture_path(original["capture_id"]) / left["file"]).convert("RGB"))
                b = np.asarray(Image.open(store.capture_path(cid) / right["file"]).convert("RGB"))
                score = similarity(a.astype(np.float64) / 255, b.astype(np.float64) / 255)
                scores.append(score)
                h, w = a.shape[:2]
                roi = [max(0, math.floor(min(left["roi"][0], right["roi"][0]))),
                       max(0, math.floor(min(left["roi"][1], right["roi"][1]))),
                       min(w, math.ceil(max(left["roi"][2], right["roi"][2]))),
                       min(h, math.ceil(max(left["roi"][3], right["roi"][3])))]
                if roi[2] - roi[0] >= 11 and roi[3] - roi[1] >= 11:
                    roi_scores.append(similarity(a[roi[1]:roi[3], roi[0]:roi[2]].astype(np.float64) / 255,
                                                 b[roi[1]:roi[3], roi[0]:roi[2]].astype(np.float64) / 255))
                exact += int(np.array_equal(a, b))
        run.update(minimum_reference_ssim=min(scores), minimum_reference_roi_ssim=min(roi_scores),
                   valid_reference_rois=len(roi_scores), exact_reference_views=exact, reference_views=len(scores))
    for first_id, second_id in zip(report["runs"][1]["capture_ids"], report["runs"][2]["capture_ids"]):
        left, right = store.capture(first_id), store.capture(second_id)
        assert left["compatibility"] == right["compatibility"]
        assert left["framing_id"] == right["framing_id"]
        assert [v["sha256"] for v in left["views"]] == [v["sha256"] for v in right["views"]], right["path"]
    report["status"] = "success" if all(min(r["minimum_reference_ssim"], r["minimum_reference_roi_ssim"]) >= .995
                                          for r in report["runs"][1:]) else "image_review_required"
    report["capture_speedup"] = statistics.median(report["runs"][0]["capture_seconds"]) / statistics.median(
        [s for run in report["runs"][1:] for s in run["capture_seconds"]])
    write_json(report_path, report)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    assert report["status"] == "success", "Review reference image differences before accepting this optimization"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--source-batch", required=True)
    parser.add_argument("--analyze-only", action="store_true")
    parser.add_argument("--report", default="acceptance_performance.json")
    run(parser.parse_args())
