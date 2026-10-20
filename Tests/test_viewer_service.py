import importlib
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Content/Python"))
from acc.common import PLUGIN, read_json, runtime_revision, write_json
from acc.service import Application, lock_output, make_handler


class ViewerServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with patch("acc.service.machine_info", return_value={}):
            self.app = Application(self.temp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.server.server_name = "127.0.0.1"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        self.thread.start()
        self.app.shutdown_callback = lambda: threading.Thread(target=self.server.shutdown, daemon=True).start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.app.scheduler.processing.shutdown(wait=True)
        self.app.output_lock.close()
        self.temp.cleanup()

    def post(self, path, token=None):
        request = urllib.request.Request(self.url + path, b"{}", {"Content-Type": "application/json",
            "X-ACC-Token": self.app.token if token is None else token})
        with urllib.request.urlopen(request, timeout=3) as reply:
            return json.load(reply)

    def test_health_reports_loaded_revision_even_if_disk_changes(self):
        with urllib.request.urlopen(self.url + "/api/health") as reply:
            health = json.load(reply)
        self.assertEqual(health["runtime_revision"], runtime_revision())
        with patch("acc.service.runtime_revision", return_value="changed"):
            self.assertEqual(self.app.get("/api/health", {})["runtime_revision"], health["runtime_revision"])

    def test_idle_progress_exposes_history_changes(self):
        before = self.app.get("/api/progress", {"summary": ["1"]})
        self.assertFalse(before["running"])
        from acc.common import DEFAULT_PROFILE
        batch = self.app.store.new_batch([], DEFAULT_PROFILE, {}, label="Labelled capture")
        listing = self.app.get("/api/batches", {})
        progress = self.app.get("/api/progress", {"summary": ["1"]})
        self.assertFalse(progress["running"])
        self.assertNotEqual(before["history_revision"], progress["history_revision"])
        self.assertEqual(listing["history_revision"], progress["history_revision"])
        self.assertEqual(listing["batches"][0]["batch_id"], batch["batch_id"])

    def test_movable_toggle_defaults_true_and_persists_static_selection(self):
        self.assertTrue(self.app.get("/api/state", {})["settings"]["profile"]["movable"])
        with patch.object(self.app, "editor", return_value={"assets": []}), \
                patch.object(self.app.scheduler, "start", return_value={"batch_id": "fixture"}) as start:
            self.app.post("/api/capture", {"paths": ["/Game/Cube.Cube"], "profile": {"movable": False}})
        self.assertFalse(start.call_args.args[1]["movable"])
        self.assertFalse(read_json(self.app.settings_file)["profile"]["movable"])

    def test_shutdown_requires_token_and_rejects_running_jobs(self):
        with self.assertRaises(urllib.error.HTTPError) as invalid:
            self.post("/api/shutdown", token="invalid")
        self.assertEqual(invalid.exception.code, 403)
        for component in (self.app.scheduler, self.app.calibration):
            component.thread = Mock()
            component.thread.is_alive.return_value = True
            with self.assertRaises(urllib.error.HTTPError) as busy:
                self.post("/api/shutdown")
            self.assertEqual(busy.exception.code, 400)
            component.thread = None
        self.assertFalse(self.app.shutting_down)
        self.assertEqual(self.post("/api/shutdown"), {"ok": True})
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())
        with self.assertRaisesRegex(ValueError, "shutting down"):
            self.app.post("/api/capture", {})

    def test_old_service_is_reported_without_launching_a_duplicate(self):
        with patch.dict(sys.modules, {"unreal": Mock()}):
            editor = importlib.import_module("acc.editor")
        response = io.BytesIO(json.dumps({"ok": True, "application": "AssetCaptureCompare"}).encode())
        with patch.object(editor, "bridge_dir", return_value=Path(self.temp.name)), \
                patch.object(editor, "read_json", return_value={"url": self.url, "pid": 123}), \
                patch("urllib.request.urlopen", return_value=response), patch.object(editor.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(RuntimeError, "older viewer.*123"):
                editor.open_window()
            launch.assert_not_called()


class ViewerProcessTests(unittest.TestCase):
    def test_idle_service_exits_releases_output_and_can_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root, bridge = Path(directory) / "output", Path(directory) / "bridge"
            command = [sys.executable, str(PLUGIN / "Scripts/serve.py"), "--root", str(root),
                       "--bridge", str(bridge), "--no-browser"]
            for _ in range(2):
                process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    deadline = time.monotonic() + 15
                    service = {}
                    while time.monotonic() < deadline:
                        service = read_json(bridge / "service.json", {})
                        if service.get("pid") == process.pid and not service.get("stopped"):
                            break
                        if process.poll() is not None:
                            self.fail("Viewer exited during startup")
                        time.sleep(.05)
                    self.assertEqual(service.get("pid"), process.pid)
                    request = urllib.request.Request(service["url"] + "/api/shutdown", b"{}",
                        {"Content-Type": "application/json", "X-ACC-Token": service["token"]})
                    with urllib.request.urlopen(request, timeout=3) as reply:
                        self.assertTrue(json.load(reply)["ok"])
                    self.assertEqual(process.wait(timeout=10), 0)
                    self.assertTrue(read_json(bridge / "service.json")["stopped"])
                    output_lock = lock_output(root)
                    output_lock.close()
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
