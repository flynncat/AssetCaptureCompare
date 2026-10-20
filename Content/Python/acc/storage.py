import copy
import shutil
import threading
import uuid
from pathlib import Path
from .common import contained, digest, file_hash, read_json, uid, utcnow, write_json


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        project = read_json(self.root / "project.json")
        if not project:
            project = {"schema": 1, "project_id": str(uuid.uuid4()), "created_at": utcnow()}
            write_json(self.root / "project.json", project)
        self.project_id = project["project_id"]
        self.rebuild()

    def rebuild(self):
        with self.lock:
            batches = []
            for path in (self.root / "batches").glob("*/batch.json"):
                try:
                    value = read_json(path)
                    if value and value["batch_id"] == path.parent.name:
                        batches.append(value)
                except (ValueError, KeyError):
                    continue
            batches.sort(key=lambda b: (b["created_at"], b["batch_id"]), reverse=True)
            self.batches = batches
            write_json(self.root / "index.json", {"schema": 1, "batches": batches})
            return copy.deepcopy(batches)

    def capture_path(self, capture_id):
        parts = str(capture_id).split("/")
        if len(parts) != 2 or any(not p or not all(c.isalnum() or c in "_-" for c in p) for p in parts):
            raise ValueError("Invalid capture ID")
        return contained(self.root, "batches/{}/assets/{}".format(*parts))

    def capture(self, capture_id):
        capture = read_json(self.capture_path(capture_id) / "capture.json")
        if not capture:
            raise FileNotFoundError("Capture has been deleted or has not completed")
        return capture

    def latest(self, identity):
        for batch in self.batches:
            if batch["status"] == "running":
                continue
            for asset in batch["assets"]:
                if asset["identity"] == identity and asset.get("status") == "success":
                    try:
                        capture = self.capture(asset["capture_id"])
                        if self.verify(capture):
                            return capture
                    except (OSError, ValueError, KeyError):
                        continue
        return None

    def verify(self, capture):
        if capture.get("status") != "success":
            return False
        directory = self.capture_path(capture["capture_id"])
        for view in capture["views"]:
            path = contained(directory, view["file"])
            if not path.is_file() or file_hash(path) != view["sha256"]:
                return False
        sheet = directory / "contact_sheet.png"
        return sheet.is_file() and file_hash(sheet) == capture["sheet_sha256"]

    def new_batch(self, assets, profile, context, label="", retry_of=None):
        with self.lock:
            batch_id = uid()
            batch = {"schema": 1, "batch_id": batch_id, "created_at": utcnow(), "label": label or batch_id,
                     "status": "running", "assets": [], "profile": profile, "context": context, "retry_of": retry_of}
            for asset in assets:
                identity = self.project_id + ":" + asset["path"]
                asset_id = digest(identity)[:24]
                baseline = self.latest(identity)
                batch["assets"].append(dict(asset, identity=identity, asset_id=asset_id,
                    capture_id=batch_id + "/" + asset_id, status="queued",
                    baseline_id=baseline["capture_id"] if baseline else None))
            self.save_batch(batch)
            write_json(self.root / "batches" / batch_id / "capture_profile.json", profile)
            return batch

    def save_batch(self, batch):
        with self.lock:
            write_json(contained(self.root, "batches/{}/batch.json".format(batch["batch_id"])), batch)
            self.batches = [b for b in self.batches if b["batch_id"] != batch["batch_id"]]
            self.batches.append(copy.deepcopy(batch))
            self.batches.sort(key=lambda b: (b["created_at"], b["batch_id"]), reverse=True)
            write_json(self.root / "index.json", {"schema": 1, "batches": self.batches})

    def batch(self, batch_id):
        value = read_json(contained(self.root, "batches/{}/batch.json".format(batch_id)))
        if not value:
            raise FileNotFoundError("Batch does not exist")
        return value

    def delete_batch(self, batch_id):
        with self.lock:
            batch = self.batch(batch_id)
            if batch["status"] == "running":
                raise ValueError("Cannot delete a running batch")
            target = contained(self.root, "batches/" + batch_id)
            if target.parent != self.root / "batches":
                raise ValueError("Invalid batch directory")
            trash = contained(self.root, ".trash/" + uid())
            trash.parent.mkdir(exist_ok=True)
            target.rename(trash)
            for path in (self.root / "comparisons").glob("*/comparison.json"):
                value = read_json(path, {})
                if any(str(value.get(key, "")).startswith(batch_id + "/") for key in ("left", "right")):
                    shutil.rmtree(str(path.parent))
            self.rebuild()
            shutil.rmtree(str(trash))

    def finish_asset(self, batch, asset, capture, staging=None):
        with self.lock:
            destination = self.capture_path(asset["capture_id"])
            if destination.exists():
                raise RuntimeError("Refusing to overwrite an immutable capture")
            pending = Path(staging) if staging else destination.parent.parent / ".staging" / ("record_" + uid())
            pending.mkdir(parents=True, exist_ok=True)
            write_json(pending / "capture.json", capture)
            destination.parent.mkdir(parents=True, exist_ok=True)
            pending.rename(destination)
            asset["status"] = capture["status"]
            asset["error"] = capture.get("error")
            asset["finished_at"] = capture.get("finished_at", utcnow())
            self.save_batch(batch)
