import argparse
import copy
import json
import mimetypes
import os
import threading
import time
import uuid
import webbrowser
from io import BytesIO
from PIL import Image
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from .common import DEFAULT_PROFILE, PLUGIN, contained, read_json, runtime_revision, write_json
from .scheduler import Scheduler, machine_info, recover_workers
from .storage import Store
from .calibration import Calibration


def lock_output(root):
    directory = Path(root).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory / ".service.lock").open("a+b")
    if handle.seek(0, 2) == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        handle.close()
        raise ValueError("This output directory is already open in another viewer; use its existing URL") from error
    return handle


class Application:
    def __init__(self, root, bridge=None):
        self.runtime_revision = runtime_revision()
        self.shutdown_callback = None
        self.operation_lock = threading.RLock()
        self.shutting_down = False
        self.preparation = None
        self.preparation_cancelled = threading.Event()
        self.output_lock = lock_output(root)
        self.store = Store(root)
        self.scheduler = Scheduler(self.store)
        self.calibration = Calibration(self.scheduler, self.apply_recommendation)
        self.bridge = Path(bridge).resolve() if bridge else None
        self.settings_file = self.bridge / "settings.json" if self.bridge else self.store.root / "settings.json"
        self.settings = read_json(self.settings_file, {"concurrency": 1, "profile": copy.deepcopy(DEFAULT_PROFILE), "scan_path": "/Game"})
        self.settings["profile"] = dict(DEFAULT_PROFILE, **self.settings.get("profile", {}))
        if not self.settings.get("performance_defaults_v1"):
            if self.settings["profile"]["warmup_frames"] == 16:
                self.settings["profile"]["warmup_frames"] = 0
            self.settings["performance_defaults_v1"] = True
            write_json(self.settings_file, self.settings)
        self.machine = machine_info()
        self.token = uuid.uuid4().hex
        recover_workers(self.store)

    def apply_recommendation(self, count):
        self.settings["concurrency"] = count
        write_json(self.settings_file, self.settings)

    def editor_state(self):
        state = read_json(self.bridge / "editor.json", {}) if self.bridge else {}
        state["connected"] = time.time() - state.get("heartbeat", 0) < 6
        return state

    def editor(self, action, cancel_event=None, **data):
        state = self.editor_state()
        if not state["connected"]:
            raise ValueError("Editor is disconnected. Open the plugin from Unreal to capture assets.")
        request_id = uuid.uuid4().hex
        request = dict(data, action=action, session=state["session"])
        request_path = self.bridge / "requests" / (request_id + ".json")
        response_path = self.bridge / "responses" / request_path.name
        write_json(request_path, request)
        try:
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                if cancel_event and cancel_event.is_set():
                    request_path.with_suffix(".cancel").touch()
                    raise InterruptedError("Capture cancelled during preparation")
                if action == "prepare":
                    progress = read_json(request_path.with_suffix(".progress"))
                    if progress and self.preparation:
                        self.preparation.update(progress)
                response = read_json(response_path)
                if response:
                    if not response["ok"]:
                        raise ValueError(response["error"])
                    return response["data"]
                time.sleep(0.1)
            raise TimeoutError("Editor operation timed out")
        finally:
            for path in (request_path, response_path, request_path.with_suffix(".progress")):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

    def get(self, route, query):
        if route == "/api/health":
            return {"ok": True, "application": "AssetCaptureCompare", "pid": os.getpid(),
                    "runtime_revision": self.runtime_revision}
        if route == "/api/state":
            return {"settings": self.settings, "machine": self.machine, "editor": self.editor_state(),
                    "root": str(self.store.root), "calibration": read_json(self.store.root / "calibration.json")}
        if route == "/api/batches":
            return {"batches": self.store.listing()}
        if route == "/api/progress":
            value = self.scheduler.progress(summary=query.get("summary") == ["1"])
            if self.preparation:
                value.update(preparation=dict(self.preparation, elapsed_seconds=time.monotonic() - self.preparation["started"]),
                             cancelling=self.preparation_cancelled.is_set())
            return value
        if route == "/api/calibration":
            return {"running": self.calibration.running, "phase": self.calibration.phase, "result": self.calibration.result}
        if route == "/api/capture":
            return self.store.capture(query["id"][0])
        if route == "/api/batch":
            return self.store.batch(query["id"][0])
        if route == "/api/batch/size":
            batch = self.store.batch(query["id"][0])
            directory = self.store.root / "batches" / batch["batch_id"]
            return {"bytes": sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())}
        raise FileNotFoundError("Unknown endpoint")

    def post(self, route, data):
        # Cancellation must not queue behind a prepare, compare or filesystem operation.
        if route in ("/api/cancel", "/api/calibrate/cancel"):
            self.preparation_cancelled.set()
            self.calibration.cancel()
            return {"ok": True}
        with self.operation_lock:
            if self.shutting_down:
                raise ValueError("The viewer is shutting down; reopen it from Unreal")
            return self._post(route, data)

    def _post(self, route, data):
        if route == "/api/shutdown":
            with self.scheduler.guard:
                if self.scheduler.running or self.calibration.running:
                    raise ValueError("Wait for the current Capture or calibration before restarting the viewer")
                if self.shutdown_callback is None:
                    raise ValueError("Viewer shutdown is unavailable")
                self.shutting_down = True
                self.shutdown_callback()
            return {"ok": True}
        if route == "/api/editor":
            if data.get("action") not in ("selection", "scan"):
                raise ValueError("Unsupported editor request")
            return self.editor(**data)
        if route == "/api/capture":
            if self.scheduler.running or self.calibration.running:
                raise ValueError("A Capture batch is already running")
            profile = copy.deepcopy(DEFAULT_PROFILE)
            profile.update(data.get("profile", {}))
            profile["renderer"] = DEFAULT_PROFILE["renderer"]
            from .common import validate_profile
            validate_profile(profile)
            count = data.get("concurrency", self.settings["concurrency"])
            self.begin_preparation(len(data["paths"]))
            try:
                prepared = self.editor("prepare", cancel_event=self.preparation_cancelled, paths=data["paths"], scene=profile["scene"])
                batch = self.scheduler.start(prepared, profile, count, data.get("label", ""), data.get("retry_of"),
                                             self.preparation_cancelled, self.preparation_progress)
            finally:
                self.preparation = None
            self.settings.update(concurrency=count, profile=profile)
            write_json(self.settings_file, self.settings)
            return {"batch_id": batch["batch_id"]}
        if route == "/api/retry":
            batch = self.store.batch(data["batch_id"])
            paths = [a["path"] for a in batch["assets"] if a["status"] in ("failed", "cancelled", "interrupted")]
            if not paths or batch["status"] == "running":
                raise ValueError("No completed failed items to retry")
            return self.post("/api/capture", {"paths": paths, "profile": batch["profile"],
                "concurrency": data.get("concurrency", self.settings["concurrency"]),
                "label": batch["label"] + " / Retry", "retry_of": batch["batch_id"]})
        if route == "/api/cancel":
            self.scheduler.cancel()
            return {"ok": True}
        if route == "/api/calibrate":
            if self.scheduler.running or self.calibration.running:
                raise ValueError("A Capture or calibration is already running")
            profile = copy.deepcopy(DEFAULT_PROFILE)
            profile.update(data.get("profile", {}))
            profile["renderer"] = DEFAULT_PROFILE["renderer"]
            from .common import validate_profile
            validate_profile(profile)
            self.begin_preparation(len(data["paths"]))
            try:
                prepared = self.editor("prepare", cancel_event=self.preparation_cancelled, paths=data["paths"],
                                       scene=profile["scene"], sample_limit=50)
                if self.preparation_cancelled.is_set():
                    raise InterruptedError("Calibration cancelled during preparation")
                self.calibration.start(prepared, profile, data.get("max_workers", 4), self.preparation_cancelled)
            finally:
                self.preparation = None
            return {"ok": True}
        if route == "/api/calibrate/cancel":
            self.calibration.cancel()
            return {"ok": True}
        if route == "/api/compare":
            with self.store.lock:
                return self.scheduler.comparer.compare(data["left"], data["right"], data.get("thresholds"), include_sheets=True)
        if route == "/api/delete":
            self.store.delete_batch(data["batch_id"])
            return {"ok": True}
        if route == "/api/output":
            if self.scheduler.running or self.calibration.running:
                raise ValueError("Wait for the current batch before switching output directories")
            root = Path(data["root"]).expanduser().resolve()
            if root == self.store.root:
                return {"root": str(root)}
            output_lock = lock_output(root)
            try:
                store = Store(root)
                recover_workers(store)
            except Exception:
                output_lock.close()
                raise
            self.scheduler.processing.shutdown(wait=True)
            self.output_lock.close()
            self.output_lock = output_lock
            self.store = store
            self.scheduler = Scheduler(self.store)
            self.calibration = Calibration(self.scheduler, self.apply_recommendation)
            self.settings["output_root"] = str(root)
            write_json(self.settings_file, self.settings)
            return {"root": str(root)}
        if route == "/api/reveal":
            path = self.store.root if not data.get("batch_id") else contained(self.store.root, "batches/" + data["batch_id"])
            os.startfile(str(path))
            return {"ok": True}
        raise FileNotFoundError("Unknown endpoint")

    def begin_preparation(self, total):
        self.preparation_cancelled.clear()
        self.preparation = {"phase": "dependencies", "completed": 0, "total": total, "started": time.monotonic()}

    def preparation_progress(self, phase, completed, total):
        if self.preparation:
            self.preparation.update(phase=phase, completed=completed, total=total)


def make_handler(application):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def send_json(self, value, status=200):
            body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def allowed(self):
            return self.headers.get("Host") == self.server.server_name + ":" + str(self.server.server_port)

        def do_GET(self):
            try:
                if not self.allowed():
                    raise PermissionError("Invalid host")
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                if parsed.path.startswith("/api/"):
                    self.send_json(application.get(parsed.path, query))
                    return
                if parsed.path.startswith("/files/"):
                    path = contained(application.store.root, unquote(parsed.path[len("/files/"):]))
                    if path.suffix.lower() != ".png":
                        raise FileNotFoundError("Only image files can be served")
                else:
                    path = PLUGIN / "Web/index.html" if parsed.path == "/" else contained(PLUGIN / "Web", unquote(parsed.path.lstrip("/")))
                if not path.is_file():
                    raise FileNotFoundError(str(path.name))
                content = path.read_bytes()
                if parsed.path.startswith("/files/"):
                    with Image.open(BytesIO(content)) as captured:
                        if captured.mode != "RGB":
                            opaque = BytesIO()
                            captured.convert("RGB").save(opaque, format="PNG")
                            content = opaque.getvalue()
                if path.name == "index.html":
                    content = content.replace(b"__ACC_TOKEN__", application.token.encode("ascii"))
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(str(path))[0] or "application/octet-stream")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(content)
            except (FileNotFoundError, KeyError) as error:
                self.send_json({"error": str(error)}, 404)
            except (ValueError, PermissionError) as error:
                self.send_json({"error": str(error)}, 400)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            try:
                if not self.allowed() or self.headers.get("X-ACC-Token") != application.token:
                    raise PermissionError("Invalid request token")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4 * 1024 * 1024:
                    raise ValueError("Invalid request length")
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                self.send_json(application.post(urlparse(self.path).path, data))
            except PermissionError as error:
                self.send_json({"error": str(error)}, 403)
            except FileNotFoundError as error:
                self.send_json({"error": str(error)}, 404)
            except Exception as error:
                self.send_json({"error": str(error)}, 400)
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--bridge")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    settings_file = Path(args.bridge) / "settings.json" if args.bridge else Path(args.root) / "settings.json"
    settings = read_json(settings_file, {})
    application = Application(settings.get("output_root", args.root), args.bridge)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(application))
    server.server_name = "127.0.0.1"
    application.shutdown_callback = lambda: threading.Thread(target=server.shutdown, daemon=True).start()
    url = "http://127.0.0.1:" + str(server.server_port)
    if args.bridge:
        write_json(Path(args.bridge) / "service.json", {"url": url, "pid": os.getpid(), "token": application.token})
    print(url, flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.3)
    finally:
        application.scheduler.cancel()
        if application.scheduler.thread:
            application.scheduler.thread.join(timeout=30)
        server.server_close()
        application.output_lock.close()
        if args.bridge:
            service_file = Path(args.bridge) / "service.json"
            service = read_json(service_file, {})
            if service.get("pid") == os.getpid():
                write_json(service_file, dict(service, stopped=True))


if __name__ == "__main__":
    main()
