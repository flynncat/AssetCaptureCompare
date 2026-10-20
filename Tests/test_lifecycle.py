import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Content/Python"))
from acc.common import DEFAULT_PROFILE, read_json, write_json
from acc.geometry import corners, frame, matrices, projection
from acc.scheduler import prepare_worker_user_dir, recover_workers
from acc.storage import Store
from acc.service import lock_output


class LifecycleTests(unittest.TestCase):
    def test_worker_cache_and_user_config_are_private_copies(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "Project"
            cache = project / "Intermediate/CachedAssetRegistry.bin"
            config = project / "Saved/Config/Windows/Engine.ini"
            cache.parent.mkdir(parents=True)
            config.parent.mkdir(parents=True)
            cache.write_bytes(b"original cache")
            config.write_text("[Rendering]\nSetting=1\n", encoding="utf-8")
            private = prepare_worker_user_dir(project / "Project.uproject", Path(directory) / "worker")
            private_cache = private / "Intermediate/CachedAssetRegistry.bin"
            private_cache.write_bytes(b"worker cache")
            self.assertEqual(cache.read_bytes(), b"original cache")
            self.assertEqual((private / "Saved/Config/Windows/Engine.ini").read_text(), config.read_text())

    def test_transient_windows_read_lock_is_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            write_json(path, {"phase": "capturing"})
            original = Path.read_text
            calls = []

            def locked_once(target, **kwargs):
                calls.append(target)
                if len(calls) == 1:
                    raise PermissionError("Windows replacement in progress")
                return original(target, **kwargs)

            with patch.object(Path, "read_text", locked_once):
                self.assertEqual(read_json(path)["phase"], "capturing")
            self.assertEqual(len(calls), 2)

    def test_recovery_does_not_terminate_reused_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            batch = store.new_batch([{"path": "/Game/Probe.Probe", "name": "Probe", "class": "StaticMesh"}], DEFAULT_PROFILE, {})
            job = str(Path(directory) / "worker.json")
            write_json(store.root / "batches" / batch["batch_id"] / "workers.json", [
                {"pid": 123, "created": 100, "job": job}, {"pid": 456, "created": 100, "job": job}])
            reused, owned = Mock(), Mock()
            reused.create_time.return_value = 200
            owned.create_time.return_value = 100
            for process in (reused, owned):
                process.cmdline.return_value = ["UE4Editor.exe", "-ACCWorker=" + job]
            with patch("acc.scheduler.psutil.Process", side_effect=[reused, owned]):
                recover_workers(store)
            reused.terminate.assert_not_called()
            owned.terminate.assert_called_once()
            self.assertEqual(store.batch(batch["batch_id"])["status"], "interrupted")
            self.assertEqual(store.batch(batch["batch_id"])["assets"][0]["status"], "interrupted")

    def test_saved_matrices_match_framing_projection(self):
        center, extent = [80, -20, 35], [5, 10, 15]
        framing = frame(center, extent, DEFAULT_PROFILE)
        for view in DEFAULT_PROFILE["views"]:
            transforms = matrices(framing, view)
            pixels = []
            for point in corners(center, extent):
                world = point + [1]
                camera = [sum(a * b for a, b in zip(row, world)) for row in transforms["view"]]
                clip = [sum(a * b for a, b in zip(row, camera)) for row in transforms["projection"]]
                pixels.append([(clip[0] / clip[3] + 1) * 240, (1 - clip[1] / clip[3]) * 240])
            actual = [min(p[0] for p in pixels), min(p[1] for p in pixels), max(p[0] for p in pixels), max(p[1] for p in pixels)]
            expected = projection(center, extent, framing, view, 480)
            for a, b in zip(actual, expected):
                self.assertAlmostEqual(a, b)

    def test_recovery_retains_scene_when_worker_has_not_exited(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "output")
            batch = store.new_batch([], DEFAULT_PROFILE, {})
            plugin = Path(directory) / "plugin"
            work = plugin / "Content/ACCWork" / batch["batch_id"]
            work.mkdir(parents=True)
            job = str(work / "worker.json")
            write_json(store.root / "batches" / batch["batch_id"] / "workers.json",
                       [{"pid": 123, "created": 100, "job": job}])
            process = Mock()
            process.create_time.return_value = 100
            process.cmdline.return_value = ["UE4Editor.exe", "-ACCWorker=" + job]
            process.wait.side_effect = psutil.TimeoutExpired(10, pid=123)
            with patch("acc.scheduler.PLUGIN", plugin), patch("acc.scheduler.psutil.Process", return_value=process):
                recover_workers(store)
            self.assertTrue(work.is_dir())
            self.assertTrue(store.batch(batch["batch_id"])["recovery_warnings"])

    def test_recovery_survives_locked_temporary_scene(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "output")
            batch = store.new_batch([], DEFAULT_PROFILE, {})
            plugin = Path(directory) / "plugin"
            work = plugin / "Content/ACCWork" / batch["batch_id"]
            work.mkdir(parents=True)
            with patch("acc.scheduler.PLUGIN", plugin), patch("acc.scheduler.shutil.rmtree", side_effect=PermissionError("Scene locked")):
                recover_workers(store)
            self.assertEqual(store.batch(batch["batch_id"])["status"], "interrupted")
            self.assertIn("Scene locked", store.batch(batch["batch_id"])["recovery_warnings"][0])

    def test_second_viewer_cannot_recover_an_active_viewers_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            first = lock_output(directory)
            try:
                with self.assertRaisesRegex(ValueError, "already open"):
                    lock_output(directory)
            finally:
                first.close()
            second = lock_output(directory)
            second.close()
