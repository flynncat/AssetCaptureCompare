"""Start the current project's repeatable 50-asset acceptance calibration."""
import argparse
import json
from client import Client


def spread(values, count):
    if len(values) < count:
        raise ValueError("Not enough representative assets")
    return [values[round(i * (len(values) - 1) / (count - 1))] for i in range(count)]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--directory", required=True)
    parser.add_argument("--max-workers", type=int, default=4)
    args = parser.parse_args()
    client = Client(args.url)
    assets = client.post("/api/editor", {"action": "scan", "path": args.directory})["assets"]
    sample = []
    for kind, count in (("StaticMesh", 32), ("SkeletalMesh", 10), ("Blueprint", 8)):
        sample.extend(spread([a for a in assets if a["class"] == kind], count))
    print(json.dumps(client.post("/api/calibrate", {"paths": [a["path"] for a in sample],
        "max_workers": args.max_workers}), ensure_ascii=False))
    print("Calibration started: 32 StaticMesh + 10 SkeletalMesh + 8 Blueprint; monitor /api/calibration")
