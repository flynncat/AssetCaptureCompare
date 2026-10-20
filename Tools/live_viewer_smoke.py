"""Verify the connected viewer in a deep output directory without editing source assets."""
import argparse
import json
import sys
import time
from pathlib import Path

from client import Client

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Content/Python"))
from acc.common import read_json, runtime_revision, write_json
from acc.geometry import fits
from acc.storage import Store


def wait_batch(client, batch_id):
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        batch = client.get("/api/batch?id=" + batch_id)
        if batch["status"] != "running":
            return batch
        time.sleep(2)
    client.post("/api/cancel", {})
    raise TimeoutError("Live viewer acceptance timed out")


def run(args):
    client = Client(args.url)
    health = client.get("/api/health")
    assert health["runtime_revision"] == runtime_revision(), "Viewer has stale Python code"
    state = client.get("/api/state")
    assert state["editor"]["connected"], "UE bridge is disconnected"
    assert not client.get("/api/progress")["running"], "Wait for the active batch"
    settings_file = Path(args.bridge) / "settings.json"
    settings = read_json(settings_file)
    output = Path(args.output).resolve()
    old_temporary = output / "batches" / ("x" * 29) / ".staging" / ("x" * 54) / ("capture.json." + "x" * 32 + ".tmp")
    assert len(str(old_temporary)) >= 260, "Choose an output deep enough to reproduce the old path limit"
    results = []
    captures = []
    try:
        client.post("/api/output", {"root": str(output)})
        for repeat in range(2):
            batch_id = client.post("/api/capture", {"paths": args.paths, "concurrency": 1,
                "label": "Live viewer deep-path acceptance / " + str(repeat + 1)})["batch_id"]
            print("Started: " + batch_id, flush=True)
            batch = wait_batch(client, batch_id)
            assert batch["status"] == "success", batch["assets"]
            store = Store(output)
            for asset in batch["assets"]:
                capture = store.capture(asset["capture_id"])
                assert store.verify(capture), capture["path"]
                assert capture["live_capture_worlds_after_cleanup"] == 0
                assert fits(capture["bounds"]["center"], capture["bounds"]["extent"], capture["framing"], batch["profile"])
                captures.append(capture)
            results.append(batch)
            print("Completed: " + batch_id, flush=True)
        original = {a["path"]: a for a in results[0]["assets"]}
        scores = []
        for asset in results[1]["assets"]:
            assert asset["baseline_id"] == original[asset["path"]]["capture_id"]
            assert asset["comparison"]["status"] == "success", asset["comparison"]
            scores.append(asset["comparison"]["score"])
        assert min(scores) >= .995, scores
        report = {"status": "success", "service_pid": health["pid"], "runtime_revision": health["runtime_revision"],
            "output": str(output), "old_temporary_path_length": len(str(old_temporary)),
            "batches": [b["batch_id"] for b in results], "paths": args.paths, "scores": scores,
            "world_counts": [c["live_capture_worlds_after_cleanup"] for c in captures]}
        write_json(Path(state["root"]) / "acceptance_live_viewer.json", report)
        print(json.dumps(report), flush=True)
    finally:
        if not client.get("/api/progress")["running"]:
            client.post("/api/output", {"root": state["root"]})
            write_json(settings_file, settings)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--bridge", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--paths", nargs="+", required=True)
    run(parser.parse_args())
