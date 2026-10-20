import copy
import shutil
import threading
import time
import uuid
from pathlib import Path
from .common import contained, digest, file_hash, read_json, uid, utcnow, write_json


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._flushed = {}
        self._history = None
        self._cache = {}
        project = read_json(self.root / "project.json")
        if not project:
            project = {"schema": 1, "project_id": str(uuid.uuid4()), "created_at": utcnow()}
            write_json(self.root / "project.json", project)
        self.project_id = project["project_id"]
        self.rebuild()

    def rebuild(self, write_index=True):
        with self.lock:
            batches = []
            for path in (self.root / "batches").glob("*/batch.json"):
                try:
                    stamp = path.stat().st_mtime_ns
                    cached = self._cache.get(path.parent.name)
                    value = cached[1] if cached and cached[0] == stamp else self.compact(read_json(path))
                    if value and value["batch_id"] == path.parent.name:
                        batches.append(value)
                        self._cache[path.parent.name] = (stamp, value)
                except (ValueError, KeyError):
                    continue
            batches.sort(key=lambda b: (b["created_at"], b["batch_id"]), reverse=True)
            self.batches = batches
            self._history = None
            if write_index:
                self.save_index()
            return copy.deepcopy(batches)

    @staticmethod
    def compact(batch):
        if not batch:
            return batch
        value = {k: v for k, v in batch.items() if k != "assets"}
        value["assets"] = [{k: v for k, v in a.items() if k != "files"} for a in batch["assets"]]
        return value

    def save_index(self):
        # The index locates batches; batch.json and immutable capture.json own the records.
        write_json(self.root / "index.json", {"schema": 2, "batches": [
            {k: b[k] for k in ("batch_id", "created_at", "label", "status")}
            for b in self.batches]})

    def listing(self):
        with self.lock:
            self.rebuild(write_index=False)
            result = []
            for batch in self.batches:
                value = {k: v for k, v in batch.items() if k != "assets"}
                value["assets"] = [{k: a[k] for k in (
                    "path", "name", "class", "capture_id", "status", "baseline_id", "comparison", "error") if k in a}
                    for a in batch["assets"]]
                result.append(value)
            return copy.deepcopy(result)

    def history_revision(self):
        # Poll metadata only; loading every asset record here makes large jobs stall.
        with self.lock:
            stamps = []
            for path in (self.root / "batches").glob("*/batch.json"):
                try:
                    stamp = path.stat()
                    stamps.append((path.parent.name, stamp.st_mtime_ns, stamp.st_size))
                except FileNotFoundError:
                    continue
            return digest([str(self.root), sorted(stamps)])

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

    def latest(self, identity, cancel_event=None):
        if self._history is None:
            self._history = {}
            for batch in self.batches:
                if batch["status"] != "running":
                    for asset in batch["assets"]:
                        if asset.get("status") == "success":
                            self._history.setdefault(asset["identity"], []).append(asset["capture_id"])
        for capture_id in self._history.get(identity, []):
            try:
                capture = self.capture(capture_id)
                if self.verify(capture, cancel_event):
                    return capture
            except InterruptedError:
                raise
            except (OSError, ValueError, KeyError):
                continue
        return None

    def verify(self, capture, cancel_event=None):
        if capture.get("status") != "success":
            return False
        directory = self.capture_path(capture["capture_id"])
        for view in capture["views"]:
            path = contained(directory, view["file"])
            if not path.is_file() or file_hash(path, cancel_event) != view["sha256"]:
                return False
        sheet = directory / "contact_sheet.png"
        return sheet.is_file() and file_hash(sheet, cancel_event) == capture["sheet_sha256"]

    def new_batch(self, assets, profile, context, label="", retry_of=None, cancel_event=None, on_progress=None):
        with self.lock:
            batch_id = uid()
            batch = {"schema": 1, "batch_id": batch_id, "created_at": utcnow(), "label": label or batch_id,
                     "status": "running", "assets": [], "profile": profile, "context": context, "retry_of": retry_of}
            for index, asset in enumerate(assets):
                if cancel_event and cancel_event.is_set():
                    raise InterruptedError("Capture cancelled during baseline preparation")
                if on_progress and index % 20 == 0:
                    on_progress("baselines", index, len(assets))
                identity = self.project_id + ":" + asset["path"]
                asset_id = digest(identity)[:24]
                baseline = self.latest(identity, cancel_event)
                batch["assets"].append(dict(asset, identity=identity, asset_id=asset_id,
                    capture_id=batch_id + "/" + asset_id, status="queued",
                    baseline_id=baseline["capture_id"] if baseline else None))
            # Store shared dependency paths once instead of rewriting them on every asset.
            files, file_ids, sources = [], {}, {}
            for asset in batch["assets"]:
                refs = []
                for path in asset.get("files", []):
                    if path not in file_ids:
                        file_ids[path] = len(files)
                        files.append(path)
                    refs.append(file_ids[path])
                sources[asset["asset_id"]] = refs
            write_json(self.root / "batches" / batch_id / "source_manifest.json", {"schema": 1, "files": files, "assets": sources})
            self.save_batch(batch)
            write_json(self.root / "batches" / batch_id / "capture_profile.json", profile)
            return batch

    def save_batch(self, batch, force=True):
        with self.lock:
            batch_id = batch["batch_id"]
            if not force and time.monotonic() - self._flushed.get(batch_id, 0) < 2:
                return
            value = copy.deepcopy(self.compact(batch))
            path = contained(self.root, "batches/{}/batch.json".format(batch_id))
            write_json(path, value)
            self._cache[batch_id] = (path.stat().st_mtime_ns, value)
            self._flushed[batch_id] = time.monotonic()
            self.batches = [b for b in self.batches if b["batch_id"] != batch["batch_id"]]
            self.batches.append(value)
            self.batches.sort(key=lambda b: (b["created_at"], b["batch_id"]), reverse=True)
            if batch["status"] != "running":
                self._history = None
            self.save_index()

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
            self.save_batch(batch, force=False)
