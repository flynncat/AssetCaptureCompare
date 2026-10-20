"""Protocol simulator for scheduler fault tests; never imports or launches Unreal."""
import os
import sys
import time
from pathlib import Path
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Content/Python"))
from acc.common import read_json, utcnow, write_json

job = Path(sys.argv[1])
write_json(job.with_suffix(".ready.json"), {"pid": os.getpid()})
while not job.with_suffix(".stop").exists():
    path = job.with_suffix(".task.json")
    if not path.exists():
        time.sleep(.01)
        continue
    task = read_json(path)
    path.unlink()
    write_json(job.with_suffix(".status.json"), {"phase": "capturing", "pid": os.getpid()})
    if "CrashOnce" in task["path"]:
        marker = job.parent / "crashed_once"
        if not marker.exists():
            marker.touch()
            os._exit(17)
    if "Hang" in task["path"]:
        while True:
            time.sleep(.05)
    if "TimeoutOnce" in task["path"]:
        marker = job.parent / "timed_out_once"
        if not marker.exists():
            marker.touch()
            while True:
                time.sleep(.05)
    directory = Path(task["staging"])
    (directory / "views").mkdir(parents=True)
    views = []
    for view in task["profile"]["views"]:
        file = "views/" + view["id"] + ".png"
        Image.new("RGB", (task["profile"]["size"], task["profile"]["size"]), (40, 130, 200)).save(directory / file)
        views.append({"id": view["id"], "file": file, "roi": [2, 2, 30, 30]})
    write_json(job.with_suffix(".result.json"), {"capture_id": task["capture_id"], "identity": task["identity"],
        "path": task["path"], "status": "success", "finished_at": utcnow(), "views": views,
        "compatibility": task["compatibility"], "framing_id": "simulated", "resources_verified": True})
