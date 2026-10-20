import importlib
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Content/Python"))
from acc.common import DEFAULT_PROFILE, read_json, representative_assets, write_json
from acc.scheduler import recover_workers
from acc.service import Application
from acc.storage import Store
import test_scheduler as scheduler_tests


class LargeBatchTests(unittest.TestCase):
    def test_sources_are_deduplicated_and_not_rewritten_with_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            files = ["C:/Fixtures/" + str(i) + ".uasset" for i in range(150)]
            assets = [{"path": "/Game/Asset_" + str(i), "class": "StaticMesh", "name": str(i), "files": files}
                      for i in range(6000)]
            batch = store.new_batch(assets, DEFAULT_PROFILE, {})
            root = store.root / "batches" / batch["batch_id"]
            manifest = read_json(root / "source_manifest.json")
            self.assertEqual(len(manifest["files"]), 150)
            self.assertEqual(len(manifest["assets"]), 6000)
            self.assertTrue(all("files" not in a for a in read_json(root / "batch.json")["assets"]))
            self.assertLess((store.root / "index.json").stat().st_size, 1024)
            with patch("acc.storage.write_json", wraps=write_json) as writes:
                for _ in range(10):
                    store.save_batch(batch, force=False)
                self.assertEqual(writes.call_count, 0)

    def test_listing_refreshes_external_changes_without_rewriting_history(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            batch = store.new_batch([], DEFAULT_PROFILE, {})
            batch["label"] = "External update"
            write_json(store.root / "batches" / batch["batch_id"] / "batch.json", batch)
            with patch("acc.storage.write_json", side_effect=AssertionError("GET must not rewrite metadata")):
                self.assertEqual(store.listing()[0]["label"], "External update")

    def test_calibration_sample_is_bounded_deterministic_and_covers_types(self):
        assets = [{"path": "/Game/" + str(i), "class": "StaticMesh"} for i in range(6000)]
        assets += [{"path": "/Game/BP", "class": "Blueprint"}, {"path": "/Game/SK", "class": "SkeletalMesh"}]
        sample = representative_assets(assets)
        self.assertEqual(len(sample), 50)
        self.assertEqual(sample, representative_assets(assets))
        self.assertEqual({a["class"] for a in sample}, {"StaticMesh", "Blueprint", "SkeletalMesh"})

    def test_cancel_does_not_wait_for_operation_lock(self):
        with tempfile.TemporaryDirectory() as directory, patch("acc.service.machine_info", return_value={}):
            app = Application(directory)
            finished = threading.Event()
            try:
                with app.operation_lock:
                    thread = threading.Thread(target=lambda: (app.post("/api/cancel", {}), finished.set()))
                    thread.start()
                    self.assertTrue(finished.wait(.5), "Cancel queued behind preparation")
                thread.join(1)
                self.assertTrue(app.preparation_cancelled.is_set())
                self.assertTrue(app.calibration.cancelled.is_set())
            finally:
                app.scheduler.processing.shutdown(wait=True)
                app.output_lock.close()

    def test_published_capture_survives_interruption_before_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            batch = store.new_batch([{"path": "/Game/A.A", "name": "A", "class": "StaticMesh"}], DEFAULT_PROFILE, {})
            asset = batch["assets"][0]
            store.finish_asset(batch, asset, {"status": "success", "views": [], "finished_at": "finished",
                                             "timings": {"asset_load": .02}, "capture_seconds": .5})
            self.assertEqual(read_json(store.root / "batches" / batch["batch_id"] / "batch.json")["assets"][0]["status"], "queued")
            recovered = Store(directory)
            recover_workers(recovered)
            record = recovered.batch(batch["batch_id"])["assets"][0]
            self.assertEqual(record["status"], "success")
            self.assertIsNone(record["error"])
            self.assertEqual(record["finished_at"], "finished")
            self.assertEqual(record["capture_seconds"], .5)

    def test_dependency_queries_are_shared_and_cancel_is_checked_between_steps(self):
        from unittest.mock import Mock
        with patch.dict(sys.modules, {"unreal": Mock()}):
            editor = importlib.import_module("acc.editor")
        api = Mock()
        api.SystemLibrary.get_engine_version.return_value = "test"
        graph = {"/Game/A": ["/Game/Common"], "/Game/B": ["/Game/Common"], "/Game/Common": []}
        registry = api.AssetRegistryHelpers.get_asset_registry.return_value
        registry.get_dependencies.side_effect = lambda path, options: graph[path]
        def drain(steps):
            while True:
                try:
                    next(steps)
                except StopIteration as value:
                    return value.value
        with patch.object(editor, "unreal", api), patch.object(editor, "package_files", side_effect=lambda path: [path + ".uasset"]):
            cache = {}
            self.assertEqual(len(drain(editor.dependency_steps("/Game/A.A", cache))[0]), 2)
            self.assertEqual(len(drain(editor.dependency_steps("/Game/B.B", cache))[0]), 2)
            self.assertEqual(registry.get_dependencies.call_count, 3)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "request.json"
                path.with_suffix(".cancel").touch()
                def endless():
                    while True:
                        yield
                with patch.object(editor, "bridge_dir", return_value=Path(directory)), patch.object(
                        editor, "project_dir", return_value=Path(directory)), patch.multiple(
                        editor, _preparing=(path, endless(), {}), _last_tick=0, _last_heartbeat=0):
                    editor.bridge_tick(0)
                    self.assertIsNone(editor._preparing)
                    self.assertIn("cancelled", read_json(Path(directory) / "responses/request.json")["error"])


class LargeCancellationTests(unittest.TestCase):
    setUp = scheduler_tests.SchedulerFaultTests.setUp
    tearDown = scheduler_tests.SchedulerFaultTests.tearDown
    start = scheduler_tests.SchedulerFaultTests.start
    finish = scheduler_tests.SchedulerFaultTests.finish
    wait_for_assignment = scheduler_tests.SchedulerFaultTests.wait_for_assignment
    def test_cancel_6000_assets_has_bounded_metadata_writes(self):
        batch = self.start(["Hang"] + ["Queued" + str(i) for i in range(5999)])
        self.wait_for_assignment()
        with patch("acc.storage.write_json", wraps=write_json) as writes:
            started = time.monotonic()
            self.scheduler.cancel()
            result = self.finish(batch, timeout=5)
            self.assertLess(time.monotonic() - started, 5)
            self.assertLess(writes.call_count, 10)
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(all(a["status"] == "cancelled" for a in result["assets"]))
        self.assertFalse(self.scheduler.workers[0]["process"].poll() is None)

    def test_cancel_with_image_backpressure_stops_workers_promptly(self):
        started_processing = threading.Event()
        original = self.scheduler.process_result

        def process_until_cancel(*args):
            started_processing.set()
            self.scheduler.cancel_event.wait(10)
            return original(*args)

        with patch.object(self.scheduler, "process_result", side_effect=process_until_cancel):
            batch = self.start(["Healthy" + str(i) for i in range(20)])
            self.assertTrue(started_processing.wait(5))
            self.scheduler.cancel()
            self.assertEqual(self.finish(batch, timeout=5)["status"], "cancelled")
