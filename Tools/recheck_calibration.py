"""Recheck sealed calibration images without rerunning or changing measured batches."""
import argparse
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Content/Python"))
from acc.calibration import repeatability
from acc.common import read_json, utcnow, write_json
from acc.storage import Store


def recheck(store, original):
    if original["status"] == "running":
        raise ValueError("Wait for calibration to finish")
    result = copy.deepcopy(original)
    for row in result["results"]:
        stable, exact_checks = len(row["batch_ids"]) == 3, []
        for batch_id in row["batch_ids"]:
            batch = store.batch(batch_id)
            stable &= (batch["status"] == "success" and
                       batch["context"]["actual_concurrency"] == row["concurrency"] and
                       not batch.get("metrics", {}).get("worker_restarts", 0))
            if row["concurrency"] > 1:
                stable &= batch.get("metrics", {}).get("peak_busy_workers", 0) >= row["concurrency"]
            evidence = [repeatability(store, asset) for asset in batch["assets"]]
            stable &= bool(evidence) and all(item[0] for item in evidence)
            exact_checks.append(sum(item[1] for item in evidence))
        row.update(stable=bool(stable), exact_image_checks=exact_checks)
    candidates = [row for row in result["results"] if row["stable"]]
    fastest = min((row["median_seconds"] for row in candidates), default=0)
    result["recommended"] = min((row["concurrency"] for row in candidates
                                 if row["median_seconds"] <= fastest * 1.05), default=None)
    result["status"] = "success" if result["recommended"] else "unstable"
    result["meets_speedup_target"] = any(row["speedup"] >= 1.3 for row in candidates)
    result["revalidated_at"] = utcnow()
    result["repeatability_rule"] = "ROI SSIM >= 0.995, or all verified original views byte-identical in a compatible framing/environment"
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--settings", type=Path)
    args = parser.parse_args()
    store = Store(args.root)
    original = read_json(store.root / "calibration.json")
    result = recheck(store, original)
    if args.apply:
        archive = store.root / "calibration.original.json"
        if not archive.exists():
            write_json(archive, original)
        write_json(store.root / "calibration.json", result)
        if args.settings and result["recommended"]:
            settings = read_json(args.settings, {})
            settings["concurrency"] = result["recommended"]
            write_json(args.settings, settings)
    print(json.dumps(result, ensure_ascii=False, indent=2))
