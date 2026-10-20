import copy
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Content/Python"))
from acc.common import DEFAULT_PROFILE, write_json
from acc.scheduler import Scheduler
from acc.storage import Store
from acc.compare import contact_sheet


class SimulatedScheduler(Scheduler):
    def launch(self, directory, index, batch, scene_file):
        path = directory / ("worker_{}.json".format(index))
        write_json(path, {})
        for suffix in (".ready.json", ".result.json", ".task.json", ".status.json", ".stop"):
            path.with_suffix(suffix).unlink(missing_ok=True)
        process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("fake_capture_worker.py")), str(path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        from acc.common import file_stamps
        stamps = file_stamps({p for a in batch["assets"] for p in a["files"]})
        return {"process": process, "path": path, "index": index, "asset": None, "started": time.monotonic(),
                "ready": False, "restarts": 0, "completed": 0, "base_rss": None, "source_stamps": stamps}


class SchedulerFaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.plugin = patch("acc.scheduler.PLUGIN", self.root / "Plugin")
        self.machine = patch("acc.scheduler.machine_info", return_value={"available_gib": 64, "gpu": {"name": "unavailable"}})
        self.gpu = patch("acc.scheduler.gpu_info", return_value={"name": "unavailable"})
        self.plugin.start(); self.machine.start(); self.gpu.start()
        self.scheduler = SimulatedScheduler(Store(self.root / "Output"))
        self.scene = self.root / "Scene.umap"
        self.scene.write_bytes(b"simulated scene fixture")
        self.profile = copy.deepcopy(DEFAULT_PROFILE)
        self.profile.update(size=32, columns=1, views=self.profile["views"][:1])

    def tearDown(self):
        self.scheduler.cancel()
        if self.scheduler.thread:
            self.scheduler.thread.join(15)
        self.scheduler.processing.shutdown(wait=True)
        self.plugin.stop(); self.machine.stop(); self.gpu.stop()
        self.temp.cleanup()

    def start(self, names, scene_files=None, count=1):
        assets = [{"path": "/Game/" + name + "." + name, "name": name, "class": "StaticMesh", "files": [str(self.scene)]} for name in names]
        return self.scheduler.start({"assets": assets, "scene_files": scene_files or [str(self.scene)], "scene_package": "/Game/Scene",
            "engine": "simulated", "project": "simulated.uproject", "engine_version": "test", "plugin_version": "test"}, self.profile, count)

    def finish(self, batch, timeout=15):
        self.scheduler.thread.join(timeout)
        self.assertFalse(self.scheduler.running, "Scheduler failed to terminate")
        return self.scheduler.store.batch(batch["batch_id"])

    def test_crash_retries_once_and_other_assets_complete(self):
        batch = self.finish(self.start(["CrashOnce", "Healthy"]))
        self.assertEqual(batch["status"], "success")
        self.assertEqual(batch["metrics"]["worker_restarts"], 1)

    def test_cancel_marks_inflight_and_queued_assets_cancelled(self):
        batch = self.start(["Hang", "Queued"])
        deadline = time.monotonic() + 5
        while not any(w.get("asset") for w in self.scheduler.workers) and time.monotonic() < deadline:
            time.sleep(.02)
        self.scheduler.cancel()
        batch = self.finish(batch)
        self.assertEqual(batch["status"], "cancelled")
        self.assertTrue(all(a["status"] == "cancelled" for a in batch["assets"]))

    def test_disk_failure_does_not_discard_healthy_asset(self):
        def selective_failure(directory, capture, profile, cancel_event=None):
            if "DiskFailure" in capture["path"]:
                raise PermissionError("Simulated output disk failure")
            return contact_sheet(directory, capture, profile, cancel_event)

        with patch("acc.scheduler.contact_sheet", side_effect=selective_failure):
            batch = self.finish(self.start(["DiskFailure", "Healthy"]))
        self.assertEqual(batch["status"], "partial")
        self.assertEqual([a["status"] for a in batch["assets"]], ["failed", "success"])

    def test_timeout_reclaims_task_and_retry_completes(self):
        self.profile["timeout"] = 30
        batch = self.finish(self.start(["TimeoutOnce", "Healthy"]), timeout=45)
        self.assertEqual(batch["status"], "success")
        self.assertEqual(batch["metrics"]["worker_restarts"], 1)

    def test_locked_scene_does_not_leave_completed_batch_running(self):
        with patch("acc.scheduler.shutil.rmtree", side_effect=PermissionError("Scene locked")):
            batch = self.finish(self.start(["Healthy"]))
        self.assertEqual(batch["status"], "success")
        self.assertFalse(self.scheduler.progress()["running"])
        self.assertIn("Scene locked", batch["cleanup_warnings"][0])

    def wait_for_assignment(self):
        deadline = time.monotonic() + 5
        while not any(w.get("asset") for w in self.scheduler.workers) and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(any(w.get("asset") for w in self.scheduler.workers))

    def test_original_map_edit_preserves_active_snapshot(self):
        batch = self.start(["Hang"])
        self.wait_for_assignment()
        frozen = self.root / "Output/batches" / batch["batch_id"] / "scene_snapshot/Scene.umap"
        self.scene.write_bytes(b"user saved new map")
        time.sleep(.5)
        self.assertFalse(self.scheduler.cancel_event.is_set())
        self.assertEqual(frozen.read_bytes(), b"simulated scene fixture")
        self.scheduler.cancel()
        self.assertIsNone(self.finish(batch)["error"])

    def test_shared_environment_dependency_edit_stops_batch(self):
        dependency = self.root / "Shared.uasset"
        dependency.write_bytes(b"shared environment material")
        batch = self.start(["Hang", "Queued"], [str(self.scene), str(dependency)])
        self.wait_for_assignment()
        dependency.write_bytes(b"material changed")
        result = self.finish(batch)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "Scene dependencies changed during capture")
        self.assertTrue(all(a["status"] != "success" for a in result["assets"]))

    def test_parallel_capture_records_overlapping_worker_activity(self):
        batch = self.start(["Hang_A", "Hang_B"], count=2)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if len(self.scheduler.workers) == 2 and all(w.get("phase") == "capturing" for w in self.scheduler.workers):
                break
            time.sleep(.02)
        self.assertTrue(all(w.get("phase") == "capturing" for w in self.scheduler.workers))
        time.sleep(.3)
        self.scheduler.cancel()
        result = self.finish(batch)
        self.assertEqual(result["metrics"]["peak_busy_workers"], 2)
        self.assertGreater(result["metrics"]["parallel_overlap_seconds"], 0)

    def test_asset_cache_growth_below_memory_limit_keeps_worker(self):
        real_process = __import__("psutil").Process
        def process_with_cache(pid):
            process = real_process(pid)
            process.memory_info = lambda: type("Memory", (), {"rss": 8 * 1024 ** 3})()
            return process
        original_launch = self.scheduler.launch
        def launch_with_baseline(*args):
            worker = original_launch(*args)
            worker["base_rss"] = 1024 ** 3
            return worker
        with patch("acc.scheduler.psutil.Process", side_effect=process_with_cache), patch.object(
                self.scheduler, "launch", side_effect=launch_with_baseline) as launch:
            batch = self.finish(self.start(["Healthy" + str(i) for i in range(10)]))
        self.assertEqual(batch["status"], "success")
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(batch["metrics"]["memory_recycles"], 0)
