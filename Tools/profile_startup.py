"""Extract startup milestones and long log gaps from an Unreal worker log."""
import argparse
import datetime
import json
import re
from pathlib import Path


def analyze(path):
    previous = None
    gaps, milestones = [], []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(r"\[(\d{4}\.\d{2}\.\d{2}-\d{2}\.\d{2}\.\d{2}:\d+)\]", line)
        if not match:
            continue
        timestamp = datetime.datetime.strptime(match.group(1), "%Y.%m.%d-%H.%M.%S:%f")
        if previous and (timestamp - previous[0]).total_seconds() >= .75:
            gaps.append({"seconds": (timestamp - previous[0]).total_seconds(),
                         "before": previous[1], "after": line})
        if any(marker in line for marker in ("Engine Initialization", "Worker ready:", "Generating Python API stub", "Cmd: py")):
            milestones.append(line)
        previous = timestamp, line
        if "Worker ready:" in line:
            break
    return {"log": str(path), "milestones": milestones,
            "gaps": sorted(gaps, key=lambda gap: gap["seconds"], reverse=True)[:15]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("log", type=Path)
    args = parser.parse_args()
    print(json.dumps(analyze(args.log), ensure_ascii=False, indent=2))
