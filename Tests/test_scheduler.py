import copy
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Content/Python"))
from acc.common import DEFAULT_PROFILE, write_json
from acc.scheduler import Scheduler, expansion_blocked
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
        batch = self.start(["Healthy", "Hang_A", "Hang_B"], count=2)
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

    def test_requested_limit_above_old_budget_runs_with_lower_concurrency(self):
        real_process = __import__("psutil").Process

        def process_with_memory(pid):
            process = real_process(pid)
            process.memory_info = lambda: SimpleNamespace(rss=16 * 1024 ** 3)
            return process

        with patch("acc.scheduler.psutil.virtual_memory", return_value=SimpleNamespace(
                available=25 * 1024 ** 3, total=64 * 1024 ** 3)), patch(
                "acc.scheduler.psutil.Process", side_effect=process_with_memory):
            batch = self.start(["Healthy", "Hang", "Queued"], count=4)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                progress = self.scheduler.progress(summary=True)
                if progress.get("concurrency_reason") == "RAM headroom":
                    break
                time.sleep(.02)
            self.assertEqual(progress.get("concurrency_reason"), "RAM headroom")
            self.assertEqual(progress["concurrency_limit"], 4)
            self.assertEqual(progress["actual_concurrency"], 1)
            self.assertEqual(len(self.scheduler.workers), 1)
            self.scheduler.cancel()
            result = self.finish(batch)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["assets"][0]["status"], "success")

    def test_parallel_limit_increases_gradually_and_never_exceeds_cap(self):
        with patch("acc.scheduler.psutil.virtual_memory", return_value=SimpleNamespace(
                available=64 * 1024 ** 3, total=128 * 1024 ** 3)):
            batch = self.finish(self.start(["Slow" + str(i) for i in range(80)], count=3), timeout=20)
        self.assertEqual(batch["status"], "success")
        increases = [c for c in batch["metrics"]["concurrency_changes"] if c["to"] > c["from"]]
        self.assertEqual([(c["from"], c["to"]) for c in increases], [(1, 2), (2, 3)])
        self.assertEqual(batch["metrics"]["peak_busy_workers"], 3)
        self.assertEqual(batch["context"]["requested_concurrency"], 3)
        self.assertLessEqual(batch["context"]["actual_concurrency"], 3)

    def test_concurrency_input_accepts_large_positive_limit(self):
        batch = self.finish(self.start(["Healthy"], count=1000))
        self.assertEqual(batch["status"], "success")
        self.assertEqual(batch["context"]["requested_concurrency"], 1000)
        self.assertEqual(len(self.scheduler.workers), 1)
        for invalid in (0, -1, True, 1.5):
            with self.assertRaisesRegex(ValueError, "positive integer"):
                self.start(["Healthy"], count=invalid)

    def test_resource_pressure_reduces_workers_and_recovery_can_expand_again(self):
        available = [64]
        with patch("acc.scheduler.psutil.virtual_memory", side_effect=lambda: SimpleNamespace(
                available=available[0] * 1024 ** 3, total=128 * 1024 ** 3)):
            batch = self.start(["Slow" + str(i) for i in range(100)], count=2)
            deadline = time.monotonic() + 8
            while self.scheduler.progress(summary=True).get("actual_concurrency", 0) < 2 and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertEqual(self.scheduler.progress(summary=True)["actual_concurrency"], 2)
            available[0] = 5
            while not any(w.get("resource_retired") for w in self.scheduler.workers) and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(any(w.get("resource_retired") for w in self.scheduler.workers))
            available[0] = 64
            result = self.finish(batch, timeout=20)
        self.assertEqual(result["status"], "success")
        changes = result["metrics"]["concurrency_changes"]
        self.assertEqual([(c["from"], c["to"]) for c in changes], [(1, 2), (2, 1), (1, 2)])
        self.assertEqual(result["metrics"]["peak_busy_workers"], 2)

    def test_expansion_uses_measured_memory_and_gpu_headroom(self):
        gpu = {"total_mib": 10240, "used_mib": 8000}
        self.assertIsNone(expansion_blocked(20, 8, gpu, 600))
        self.assertEqual(expansion_blocked(20, 16, gpu, 600), "RAM headroom")
        self.assertEqual(expansion_blocked(64, 16, gpu, 2000), "VRAM headroom")
        self.assertIsNone(expansion_blocked(64, 16, {"name": "unavailable"}, 2000))

    def test_calibration_skips_higher_levels_when_actual_workers_stay_lower(self):
        from acc.calibration import Calibration
        scheduler = Mock()
        scheduler.store.root = self.root
        scheduler.start.return_value = {"batch_id": "simulated"}
        scheduler.store.batch.return_value = {"batch_id": "simulated", "status": "success", "elapsed_seconds": 1,
            "context": {"actual_concurrency": 1, "machine": {}}, "metrics": {}, "assets": []}
        calibration = Calibration(scheduler)
        calibration.run({"assets": []}, self.profile, 4)
        self.assertEqual(calibration.result["recommended"], 1)
        self.assertEqual(scheduler.start.call_count, 5)
        self.assertIn("measured active workers 1", calibration.result["resource_limit"])

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
