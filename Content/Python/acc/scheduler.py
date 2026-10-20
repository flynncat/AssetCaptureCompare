import copy
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import psutil
from .common import PLUGIN, contained, digest, file_hash, file_stamps, read_json, uid, utcnow, validate_profile, write_json
from .compare import Comparer, contact_sheet
from .timings import Timings


def gpu_info():
    try:
        result = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used,driver_version", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        row = result.stdout.strip().splitlines()[0].split(",")
        return {"name": row[0].strip(), "total_mib": int(row[1]), "used_mib": int(row[2]), "driver": row[3].strip()}
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return {"name": "unavailable"}


def machine_info():
    memory = psutil.virtual_memory()
    return {"cpu_logical": psutil.cpu_count(), "cpu_physical": psutil.cpu_count(False),
            "memory_gib": round(memory.total / 1024 ** 3, 1), "available_gib": round(memory.available / 1024 ** 3, 1),
            "gpu": gpu_info()}


def expansion_blocked(available_gib, worker_peak_gib, gpu, worker_gpu_mib):
    if available_gib < 8 + worker_peak_gib * 1.2:
        return "RAM headroom"
    if "total_mib" in gpu and gpu["total_mib"] - gpu["used_mib"] < 256 + worker_gpu_mib * 1.2:
        return "VRAM headroom"
    return None


def prepare_worker_user_dir(project, directory):
    project, directory = Path(project).parent, Path(directory)
    for source in (project / "Saved/Config/Windows").glob("*.ini"):
        target = directory / "Saved/Config/Windows" / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(source), str(target))
    cache = project / "Intermediate/CachedAssetRegistry.bin"
    if cache.is_file():
        target = directory / "Intermediate" / cache.name
        target.parent.mkdir(parents=True, exist_ok=True)
        # A private copy preserves fast reads without sharing Windows file locks.
        try:
            shutil.copy2(str(cache), str(target))
        except OSError:
            target.unlink(missing_ok=True)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def recover_workers(store):
    """Only reclaim processes whose PID, creation time and job path all match."""
    for batch in store.batches:
        if batch["status"] != "running":
            continue
        manifest = store.root / "batches" / batch["batch_id"] / "workers.json"
        workers_stopped = True
        recovery_warnings = []
        for owned in read_json(manifest, []):
            try:
                process = psutil.Process(owned["pid"])
                command = process.cmdline()
                if abs(process.create_time() - owned["created"]) < 1 and any(
                        arg.lower() == ("-accworker=" + owned["job"]).lower() for arg in command):
                    process.terminate()
                    process.wait(timeout=10)
            except psutil.NoSuchProcess:
                pass
            except (psutil.Error, KeyError) as error:
                workers_stopped = False
                recovery_warnings.append("Worker recovery incomplete: " + str(error))
        for asset in batch["assets"]:
            if asset["status"] in ("queued", "capturing", "processing"):
                existing = store.capture_path(asset["capture_id"]) / "capture.json"
                capture = read_json(existing)
                asset["status"] = capture["status"] if capture else "interrupted"
                asset["error"] = capture.get("error") if capture else "Service interrupted before the batch was sealed"
                if capture:
                    asset["finished_at"] = capture.get("finished_at")
                    asset["timings"] = capture.get("timings", {})
                    asset["capture_seconds"] = capture.get("capture_seconds")
        batch.update(status="interrupted", error="Previous service stopped before sealing this batch", finished_at=utcnow())
        work = contained(PLUGIN / "Content/ACCWork", batch["batch_id"])
        if workers_stopped and work.is_dir() and work.parent == PLUGIN / "Content/ACCWork":
            try:
                shutil.rmtree(str(work))
            except OSError as error:
                recovery_warnings.append("Temporary scene retained: " + str(error))
        runtime = store.root / "batches" / batch["batch_id"] / "worker_runtime"
        if workers_stopped and runtime.is_dir():
            try:
                shutil.rmtree(str(runtime))
            except OSError as error:
                recovery_warnings.append("Worker cache retained: " + str(error))
        if recovery_warnings:
            batch["recovery_warnings"] = recovery_warnings
        store.save_batch(batch)


class Scheduler:
    def __init__(self, store):
        self.store = store
        self.comparer = Comparer(store)
        self.thread = None
        self.cancel_event = threading.Event()
        self.guard = threading.RLock()
        self.workers = []
        self.processing = ThreadPoolExecutor(max_workers=2, thread_name_prefix="acc-image")
        self.active_id = None
        self.active_batch = None
        self.cancel_requested = None
        self.phase = "idle"

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self, prepared, profile, count, label="", retry_of=None, cancel_event=None, on_progress=None):
        with self.guard:
            if self.running:
                raise ValueError("A Capture batch is already running")
            validate_profile(profile)
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise ValueError("Capture concurrency limit must be a positive integer")
            machine = machine_info()
            cache = Path(prepared["project"]).parent / "Intermediate/CachedAssetRegistry.bin"
            cache_bytes = cache.stat().st_size if cache.is_file() else 0
            if shutil.disk_usage(self.store.root).free < len(prepared["assets"]) * profile["size"] ** 2 * len(profile["views"]) * 8 + cache_bytes + 1024 ** 3:
                raise ValueError("Not enough free disk space")
            self.cancel_event.clear()
            context = {"engine": prepared["engine"], "project": prepared["project"], "engine_version": prepared["engine_version"],
                       "plugin_version": prepared["plugin_version"], "machine": machine,
                       "requested_concurrency": count, "actual_concurrency": 1}
            batch = self.store.new_batch(prepared["assets"], profile, context, label, retry_of, cancel_event, on_progress)
            if cancel_event and cancel_event.is_set():
                self.mark_remaining(batch, "Cancelled by user", "cancelled")
                batch.update(status="cancelled", finished_at=utcnow())
                self.store.save_batch(batch)
                raise InterruptedError("Capture cancelled during preparation")
            self.cancel_requested = None
            self.active_id = batch["batch_id"]
            self.active_batch = batch
            self.phase = "snapshot"
            self.thread = threading.Thread(target=self.run, args=(batch, prepared, count), daemon=True, name="acc-scheduler")
            self.thread.start()
            return batch

    def cancel(self):
        if not self.cancel_event.is_set():
            self.cancel_requested = time.monotonic()
        self.cancel_event.set()

    def mark_remaining(self, batch, error, status):
        with self.store.lock:
            finished = utcnow()
            for asset in batch["assets"]:
                if asset["status"] in ("queued", "capturing", "processing"):
                    asset.update(status=status, error=error, finished_at=finished)

    def launch(self, directory, index, batch, scene_file):
        setup_started = time.monotonic()
        source_stamps = file_stamps(sorted({p for a in batch["assets"] for p in a["files"]}))
        path = directory / ("worker_{}.json".format(index))
        write_json(path, {"scene_file": str(scene_file)})
        for suffix in (".ready.json", ".status.json", ".result.json", ".task.json", ".stop"):
            old = path.with_suffix(suffix)
            if old.exists():
                old.unlink()
        log_path = self.store.root / "batches" / batch["batch_id"] / "logs" / ("worker_{}_{}.log".format(index, uid()))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        user_dir = prepare_worker_user_dir(batch["context"]["project"],
            log_path.parent.parent / "worker_runtime" / log_path.stem)
        command = [str(Path(batch["context"]["engine"]) / "Engine/Binaries/Win64/UE4Editor.exe"), batch["context"]["project"],
            "/Engine/Maps/Entry", "-ACCWorker=" + str(path), "-unattended", "-nosplash", "-nosound",
            "-RenderOffscreen", "-d3d11", "-windowed", "-ResX=64", "-ResY=64", "-NoSourceControl", "-NoRestoreOpenAssetTabs", "-NoAutoSave",
            "-DisablePlugins=RenderDocPlugin", "-UserDir=" + str(user_dir),
            "-abslog=" + str(log_path),
            "-ini:Engine:[/Script/PythonScriptPlugin.PythonScriptPluginSettings]:bRemoteExecution=False",
            "-ini:Engine:[/Script/PythonScriptPlugin.PythonScriptPluginSettings]:bDeveloperMode=False",
            "-ini:EditorPerProjectUserSettings:[/Script/PythonScriptPlugin.PythonScriptPluginUserSettings]:bDeveloperMode=False",
            "-ini:EditorPerProjectUserSettings:[/Script/PythonScriptPlugin.PythonScriptPluginUserSettings]:bEnableContentBrowserIntegration=False"]
        setup_seconds = time.monotonic() - setup_started
        process = subprocess.Popen(command, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        manifest = self.store.root / "batches" / batch["batch_id"] / "workers.json"
        owned = read_json(manifest, [])
        owned.append({"pid": process.pid, "created": psutil.Process(process.pid).create_time(), "job": str(path)})
        write_json(manifest, owned)
        return {"process": process, "path": path, "index": index, "asset": None, "started": time.monotonic(),
                "ready": False, "restarts": 0, "completed": 0, "base_rss": None, "source_stamps": source_stamps,
                "log": str(log_path), "setup_seconds": setup_seconds}

    def fail(self, batch, asset, error, status="failed"):
        capture = {"capture_id": asset["capture_id"], "identity": asset["identity"], "path": asset["path"],
                   "class": asset["class"], "status": status, "error": error, "finished_at": utcnow(), "views": []}
        self.store.finish_asset(batch, asset, capture)

    def process_result(self, batch, asset, result, staging):
        timings = Timings()
        try:
            if self.cancel_event.is_set():
                return
            if result["status"] != "success":
                self.fail(batch, asset, result.get("error", "Capture failed"))
                return
            if len(result["views"]) != len(batch["profile"]["views"]):
                raise ValueError("Incomplete views")
            if not result.get("resources_verified"):
                raise ValueError("Native resource readiness was not verified")
            adapter = batch["context"]["machine"]["gpu"].get("name", "unavailable")
            if adapter != "unavailable" and adapter not in result.get("render_environment", ""):
                raise ValueError("Worker rendered on a different GPU: " + result.get("render_environment", ""))
            with timings.measure("image_processing"):
                contact_sheet(staging, result, batch["profile"], cancel_event=self.cancel_event)
            if self.cancel_event.is_set():
                return
            destination = self.store.capture_path(asset["capture_id"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            with timings.measure("publication"):
                self.store.finish_asset(batch, asset, result, staging)
            if asset.get("baseline_id") and not self.cancel_event.is_set():
                try:
                    with timings.measure("comparison"):
                        comparison = self.comparer.compare(asset["baseline_id"], asset["capture_id"], cancel_event=self.cancel_event)
                    asset["comparison"] = {k: comparison[k] for k in ("comparison_id", "status", "score", "category", "worst_view", "reasons") if k in comparison}
                except Exception as error:
                    asset["comparison"] = {"status": "failed", "error": str(error)}
            else:
                asset["comparison"] = {"status": "no_baseline"}
            asset["timings"] = dict(result.get("timings", {}), **timings.seconds)
            asset["capture_seconds"] = result.get("capture_seconds")
            self.store.save_batch(batch, force=False)
        except Exception as error:
            if isinstance(error, InterruptedError) and self.cancel_event.is_set():
                return
            if not self.store.capture_path(asset["capture_id"]).exists():
                self.fail(batch, asset, str(error))
            else:
                asset["comparison"] = {"status": "failed", "error": str(error)}
                self.store.save_batch(batch, force=False)

    def run(self, batch, prepared, count):
        started = time.monotonic()
        batch_dir = self.store.root / "batches" / batch["batch_id"]
        work = PLUGIN / "Content/ACCWork" / batch["batch_id"]
        futures = []
        self.workers = []
        fatal = None
        hashes = {}
        def source_version(files, stamps):
            values = {}
            for path in files:
                key = path, tuple(stamps[path])
                if key not in hashes:
                    hashes[key] = file_hash(path, self.cancel_event)
                values[path] = hashes[key]
            return digest(values)
        try:
            if self.cancel_event.is_set():
                return
            work.mkdir(parents=True)
            snapshot = batch_dir / "scene_snapshot"
            snapshot.mkdir()
            snapshot_stamps = file_stamps(prepared["scene_files"])
            scene_map = next(Path(p) for p in prepared["scene_files"] if p.endswith(".umap") and Path(p).stem == prepared["scene_package"].split("/")[-1])
            sources = [scene_map.with_suffix(e) for e in (".umap", ".uexp", ".ubulk")]
            sources.extend(scene_map.with_name(scene_map.stem + "_BuiltData").with_suffix(e) for e in (".uasset", ".uexp", ".ubulk"))
            frozen_sources = set()
            for source in sources:
                if source.exists():
                    shutil.copy2(str(source), str(snapshot / source.name))
                    shutil.copy2(str(source), str(work / source.name))
                    frozen_sources.add(source)
            scene_version = {p: file_hash(snapshot / Path(p).name if Path(p) in frozen_sources else p, self.cancel_event)
                             for p in prepared["scene_files"]}
            if file_stamps(prepared["scene_files"]) != snapshot_stamps:
                raise ValueError("Scene changed while creating its snapshot; retry Capture")
            # Map edits apply to the next batch; shared rendering dependencies stay live.
            live_scene_files = [p for p in prepared["scene_files"] if Path(p) not in frozen_sources]
            scene_stamps = file_stamps(live_scene_files)
            write_json(snapshot / "dependencies.json", scene_version)
            rendering_profile = {k: v for k, v in batch["profile"].items() if k not in ("timeout", "reframe", "worker_memory_gib")}
            # Transient usage counters are not part of the rendered environment's identity.
            gpu = dict(batch["context"]["machine"]["gpu"])
            gpu.pop("used_mib", None)
            environment = {"profile": rendering_profile, "scene": scene_version,
                "engine": batch["context"]["engine_version"], "gpu": gpu, "renderer": "D3D11"}
            compatibility = digest(environment)
            batch["compatibility"] = compatibility
            self.store.save_batch(batch)
            scene_snapshot_seconds = time.monotonic() - started
            if not self.cancel_event.is_set():
                self.workers.append(self.launch(work, 0, batch, work / scene_map.name))
            self.phase = "capturing"
            pending = list(batch["assets"])
            attempts = {}
            metrics = {"peak_worker_rss_gib": 0, "peak_gpu_used_mib": 0, "peak_system_used_gib": 0, "worker_restarts": 0,
                       "memory_recycles": 0, "worker_memory_limit_gib": batch["profile"]["worker_memory_gib"],
                       "peak_system_cpu_percent": 0, "concurrency_changes": []}
            metrics.update(peak_busy_workers=0, parallel_overlap_seconds=0.0,
                           scene_snapshot_seconds=scene_snapshot_seconds, worker_startups=[])
            sampled_at = 0
            previous_cycle = time.monotonic()
            gpu_sample = {}
            worker_peak_gib = 0
            worker_gpu_mib = 512
            gpu_baseline = batch["context"]["machine"]["gpu"].get("used_mib", 0)
            scaling_reason = "Waiting for worker measurement"
            progress_at = 0
            while pending or any(w["asset"] for w in self.workers):
                if self.cancel_event.is_set():
                    break
                for future in futures:
                    if future.done():
                        future.result()
                futures = [f for f in futures if not f.done()]
                sampled = time.monotonic() - sampled_at > 2
                if sampled:
                    sampled_at = time.monotonic()
                    rss = 0
                    for w in self.workers:
                        try:
                            worker_rss = psutil.Process(w["process"].pid).memory_info().rss
                            rss += worker_rss
                            worker_peak_gib = max(worker_peak_gib, worker_rss / 1024 ** 3)
                        except psutil.NoSuchProcess:
                            pass
                    metrics["peak_worker_rss_gib"] = max(metrics["peak_worker_rss_gib"], round(rss / 1024 ** 3, 2))
                    memory = psutil.virtual_memory()
                    metrics["peak_system_used_gib"] = max(metrics["peak_system_used_gib"], round((memory.total - memory.available) / 1024 ** 3, 2))
                    metrics["peak_system_cpu_percent"] = max(metrics["peak_system_cpu_percent"], psutil.cpu_percent())
                    gpu_sample = gpu_info()
                    metrics["peak_gpu_used_mib"] = max(metrics["peak_gpu_used_mib"], gpu_sample.get("used_mib", 0))
                    live = sum(w["process"].poll() is None for w in self.workers)
                    if live and "used_mib" in gpu_sample:
                        worker_gpu_mib = max(worker_gpu_mib, (gpu_sample["used_mib"] - gpu_baseline) / live)
                if file_stamps(live_scene_files) != scene_stamps:
                    fatal = "Scene dependencies changed during capture"
                    self.cancel_event.set()
                for worker in self.workers:
                    if worker.get("disabled"):
                        continue
                    process, path, asset = worker["process"], worker["path"], worker["asset"]
                    status = read_json(path.with_suffix(".status.json"), {})
                    worker["phase"] = status.get("phase", "starting")
                    if process.poll() is not None:
                        if asset:
                            key = asset["capture_id"]
                            attempts[key] = attempts.get(key, 0) + 1
                            if attempts[key] <= 1 and not self.cancel_event.is_set():
                                asset["status"] = "queued"
                                pending.insert(0, asset)
                            else:
                                cancelled = self.cancel_event.is_set() and not fatal
                                self.fail(batch, asset, "Cancelled by user" if cancelled else "Worker exited: {}".format(process.returncode),
                                          "cancelled" if cancelled else "failed")
                        worker["asset"] = None
                        if not self.cancel_event.is_set() and pending and worker["restarts"] < 2:
                            replacement = self.launch(work, worker["index"], batch, work / scene_map.name)
                            replacement["restarts"] = worker["restarts"] + 1
                            metrics["worker_restarts"] += 1
                            worker.update(replacement)
                        else:
                            worker["disabled"] = True
                        continue
                    ready = path.with_suffix(".ready.json").exists()
                    if not ready:
                        if time.monotonic() - worker["started"] > 300:
                            process.kill()
                        continue
                    if not worker["ready"]:
                        worker["ready"] = True
                        metrics["worker_startups"].append({"index": worker["index"], "pid": process.pid,
                            "seconds": time.monotonic() - worker["started"], "log": worker.get("log"),
                            "setup_seconds": worker.get("setup_seconds", 0)})
                    if worker["base_rss"] is None:
                        worker["base_rss"] = psutil.Process(process.pid).memory_info().rss
                        if worker["base_rss"] > batch["profile"]["worker_memory_gib"] * 1024 ** 3:
                            worker["disabled"] = True
                            process.terminate()
                            fatal = "Worker startup memory exceeds its configured memory limit"
                            continue
                    result_path = path.with_suffix(".result.json")
                    if asset and result_path.exists():
                        result = read_json(result_path)
                        result_path.unlink()
                        if result.get("capture_id") != asset["capture_id"]:
                            raise RuntimeError("Worker returned a result for a different asset")
                        if fatal:
                            result.update(status="failed", error=fatal)
                        asset["status"] = "processing"
                        futures.append(self.processing.submit(self.process_result, batch, asset, result, worker["staging"]))
                        worker["asset"] = None
                        worker["completed"] += 1
                        # Limit pending full-resolution images rather than building an unbounded queue.
                    if self.cancel_event.is_set():
                        break
                    elif worker["asset"] and time.monotonic() - worker["assigned"] > batch["profile"]["timeout"]:
                        process.kill()
                    if worker["asset"] is None and pending and not self.cancel_event.is_set():
                        if len(futures) >= max(2, sum(not w.get("disabled") for w in self.workers)):
                            worker["phase"] = "processing_images"
                            continue
                        healthy = sum(not w.get("disabled") for w in self.workers)
                        gpu_pressure = gpu_sample.get("used_mib", 0) > gpu_sample.get("total_mib", 1024 * 1024) - 128
                        if healthy > 1 and (psutil.virtual_memory().available < 6 * 1024 ** 3 or gpu_pressure):
                            path.with_suffix(".stop").touch()
                            worker["disabled"] = True
                            worker["resource_retired"] = True
                            metrics["concurrency_changes"].append({"elapsed_seconds": time.monotonic() - started,
                                "from": healthy, "to": healthy - 1, "reason": "RAM/VRAM pressure"})
                            batch["context"]["actual_concurrency"] = healthy - 1
                            continue
                        rss = psutil.Process(process.pid).memory_info().rss
                        worker_peak_gib = max(worker_peak_gib, rss / 1024 ** 3)
                        next_stamps = file_stamps(pending[0]["files"])
                        changed_source = any(worker["source_stamps"].get(p) != stamp for p, stamp in next_stamps.items())
                        if rss > batch["profile"]["worker_memory_gib"] * 1024 ** 3 or changed_source:
                            path.with_suffix(".stop").touch()
                            try:
                                process.wait(timeout=10)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait(timeout=10)
                            worker.update(self.launch(work, worker["index"], batch, work / scene_map.name))
                            key = "source_reloads" if changed_source else "memory_recycles"
                            metrics[key] = metrics.get(key, 0) + 1
                            continue
                        asset = pending.pop(0)
                        staging = batch_dir / ".staging" / uid()
                        before = file_stamps(asset["files"])
                        if any(stamp is None for stamp in before.values()):
                            self.fail(batch, asset, "Source or dependency was deleted before capture")
                            continue
                        baseline = None
                        if asset.get("baseline_id"):
                            try:
                                baseline = self.store.capture(asset["baseline_id"])
                                if baseline.get("compatibility") != compatibility:
                                    baseline = None
                            except FileNotFoundError:
                                pass
                        task = dict(asset, profile=batch["profile"], staging=str(staging), compatibility=compatibility,
                            compatibility_details=environment, source_stamps=before,
                            source_version=source_version(asset["files"], before), baseline=baseline)
                        worker.update(asset=asset, staging=staging, assigned=time.monotonic())
                        asset.update(status="capturing", worker=worker["index"], started_at=utcnow())
                        write_json(path.with_suffix(".task.json"), task)
                        self.store.save_batch(batch, force=False)
                if pending and all(w.get("disabled") for w in self.workers):
                    fatal = "No healthy capture workers remain"
                    self.cancel_event.set()
                if self.cancel_event.is_set():
                    break
                healthy = [w for w in self.workers if not w.get("disabled")]
                live_count = sum(w["process"].poll() is None for w in self.workers)
                # Measure loaded workers before admitting one more editor process.
                if sampled and pending and len(healthy) < count:
                    scaling_reason = "Waiting for worker measurement"
                    if live_count == len(healthy) and all(w["ready"] and w["completed"] for w in healthy):
                        scaling_reason = expansion_blocked(psutil.virtual_memory().available / 1024 ** 3,
                                                           worker_peak_gib, gpu_sample, worker_gpu_mib)
                        cache = Path(prepared["project"]).parent / "Intermediate/CachedAssetRegistry.bin"
                        cache_bytes = cache.stat().st_size if cache.is_file() else 0
                        if not scaling_reason and shutil.disk_usage(self.store.root).free < cache_bytes + 1024 ** 3:
                            scaling_reason = "Disk headroom"
                        if not scaling_reason and len(futures) < max(2, len(healthy)):
                            reusable = next((w for w in self.workers if w.get("resource_retired")), None)
                            index = reusable["index"] if reusable else len(self.workers)
                            added = self.launch(work, index, batch, work / scene_map.name)
                            if reusable:
                                reusable.clear()
                                reusable.update(added)
                            else:
                                self.workers.append(added)
                            metrics["concurrency_changes"].append({"elapsed_seconds": time.monotonic() - started,
                                "from": len(healthy), "to": len(healthy) + 1, "reason": "Measured resource headroom"})
                            batch["context"]["actual_concurrency"] = max(batch["context"]["actual_concurrency"], len(healthy) + 1)
                            self.store.save_batch(batch, force=False)
                        elif not scaling_reason:
                            scaling_reason = "Image processing backpressure"
                now = time.monotonic()
                busy = sum(bool(w["asset"]) and not w.get("disabled") and w.get("phase") not in ("starting", "idle")
                           for w in self.workers)
                metrics["peak_busy_workers"] = max(metrics["peak_busy_workers"], busy)
                if busy > 1:
                    metrics["parallel_overlap_seconds"] += now - previous_cycle
                previous_cycle = now
                if now - progress_at >= .2:
                    progress_at = now
                    write_json(batch_dir / "progress.json", {"workers": [{"index": w["index"], "pid": w["process"].pid,
                        "phase": w.get("phase"), "startup_seconds": now - w["started"] if not w["ready"] else None,
                        "path": w["asset"]["path"] if w["asset"] else None} for w in self.workers if not w.get("disabled")],
                        "concurrency_reason": scaling_reason if pending and len(healthy) < count else None,
                        "elapsed_seconds": time.monotonic() - started})
                self.cancel_event.wait(0.02)
        except Exception as error:
            if not isinstance(error, InterruptedError) or not self.cancel_event.is_set():
                fatal = str(error)
        finally:
            self.phase = "cancelling" if self.cancel_event.is_set() else "finalizing"
            for worker in self.workers:
                worker["path"].with_suffix(".stop").touch()
                if self.cancel_event.is_set() and worker["process"].poll() is None:
                    worker["process"].terminate()
            stop_deadline = time.monotonic() + (3 if self.cancel_event.is_set() else 10)
            for worker in self.workers:
                process = worker["process"]
                try:
                    process.wait(timeout=max(.01, stop_deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
            for future in futures:
                if self.cancel_event.is_set():
                    future.cancel()
                try:
                    if future.cancelled():
                        continue
                    future.result()
                except Exception as error:
                    fatal = str(error)
            self.mark_remaining(batch, fatal or "Cancelled by user", "failed" if fatal else "cancelled")
            success = sum(a["status"] == "success" for a in batch["assets"])
            batch.update(status="cancelled" if self.cancel_event.is_set() and not fatal else "success" if success == len(batch["assets"]) else "partial" if success else "failed",
                finished_at=utcnow(), elapsed_seconds=time.monotonic() - started, error=fatal)
            if "metrics" in locals():
                batch["metrics"] = metrics
            try:
                runtime = batch_dir / "worker_runtime"
                if runtime.is_dir():
                    try:
                        shutil.rmtree(str(runtime))
                    except OSError as error:
                        batch.setdefault("cleanup_warnings", []).append("Worker cache retained: " + str(error))
                if work.exists() and work.parent == PLUGIN / "Content/ACCWork":
                    try:
                        shutil.rmtree(str(work))
                    except OSError as error:
                        batch.setdefault("cleanup_warnings", []).append("Temporary scene retained: " + str(error))
                if self.cancel_requested is not None:
                    batch["cancellation_seconds"] = time.monotonic() - self.cancel_requested
                self.store.save_batch(batch)
            finally:
                self.active_id = None
                self.active_batch = None
                self.phase = "idle"

    def progress(self, summary=False):
        if not self.active_id:
            return {"running": False}
        batch = self.active_batch
        progress = read_json(self.store.root / "batches" / self.active_id / "progress.json", {})
        if not batch:
            return {"running": False}
        completed = sum(a["status"] in ("success", "failed", "cancelled", "interrupted") for a in batch["assets"])
        value = dict(progress, running=True, batch_id=batch["batch_id"], total=len(batch["assets"]), completed=completed,
                     phase=self.phase, cancelling=self.cancel_event.is_set(),
                     concurrency_limit=batch["context"]["requested_concurrency"],
                     actual_concurrency=sum(not w.get("disabled") and w["process"].poll() is None for w in self.workers))
        if not summary:
            value["batch"] = copy.deepcopy(self.store.compact(batch))
        return value
