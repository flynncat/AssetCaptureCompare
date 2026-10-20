"""Editor entry; all Unreal calls are confined to Slate ticks."""
import copy
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import uuid
import webbrowser
from pathlib import Path
import unreal
from .common import DEFAULT_PROFILE, PLUGIN, digest, read_json, representative_assets, runtime_revision, utcnow, write_json

_handle = None
_last_tick = 0
_mounts = None
_server = None
_session = uuid.uuid4().hex
_busy = False
_preparing = None
_last_heartbeat = 0


def project_dir():
    return Path(unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_dir())).resolve()


def bridge_dir():
    return project_dir() / "Saved/AssetCaptureCompareBridge"


def package_files(package):
    global _mounts
    if _mounts is None:
        engine = Path(unreal.Paths.convert_relative_path_to_full(unreal.Paths.engine_dir())).resolve()
        _mounts = {"Game": project_dir() / "Content", "Engine": engine / "Content", "AssetCaptureCompare": PLUGIN / "Content"}
        for root in (engine / "Plugins", project_dir() / "Plugins"):
            for descriptor in root.rglob("*.uplugin"):
                _mounts.setdefault(descriptor.stem, descriptor.parent / "Content")
    parts = str(package).lstrip("/").split("/", 1)
    if len(parts) != 2 or parts[0] not in _mounts:
        return []
    base = _mounts[parts[0]] / parts[1]
    return [str(base.with_suffix(extension)) for extension in (".uasset", ".umap", ".uexp", ".ubulk") if base.with_suffix(extension).is_file()]


def dependency_steps(path, cache):
    registry = unreal.AssetRegistryHelpers.get_asset_registry()
    options = unreal.AssetRegistryDependencyOptions(include_hard_package_references=True, include_soft_package_references=True)
    pending, seen, files = [path.split(".")[0]], set(), set()
    while pending:
        package = pending.pop()
        if package in seen or package.startswith("/Script/"):
            continue
        seen.add(package)
        if package not in cache:
            cache[package] = (package_files(package), [str(p) for p in (registry.get_dependencies(package, options) or [])])
        package_sources, references = cache[package]
        files.update(package_sources)
        pending.extend(p for p in references if p not in seen)
        yield
    return sorted(files), seen


def dependencies(path):
    steps = dependency_steps(path, {})
    while True:
        try:
            next(steps)
        except StopIteration as result:
            return result.value


def prepare_steps(request):
    registry = unreal.AssetRegistryHelpers.get_asset_registry()
    paths = list(dict.fromkeys(request["paths"]))
    if not paths or len(paths) > 10000:
        raise ValueError("Select 1-10000 assets")
    selection_count = len(paths)
    if request.get("sample_limit"):
        values = []
        for path in paths:
            data = registry.get_asset_by_object_path(path)
            if data.is_valid():
                values.append(describe(data))
            yield
        paths = [a["path"] for a in representative_assets(values, int(request["sample_limit"]))]
    dirty = {p.get_name() for p in unreal.EditorLoadingAndSavingUtils.get_dirty_content_packages()}
    dirty.update(p.get_name() for p in unreal.EditorLoadingAndSavingUtils.get_dirty_map_packages())
    assets, touched, cache = [], set(), {}
    for index, path in enumerate(paths):
        yield {"phase": "dependencies", "completed": index, "total": len(paths), "path": path}
        data = registry.get_asset_by_object_path(path)
        if not data.is_valid() or str(data.asset_class) not in ("StaticMesh", "SkeletalMesh", "Blueprint"):
            raise ValueError("Unsupported or missing asset: " + path)
        files, packages = yield from dependency_steps(path, cache)
        if not files:
            raise ValueError("Asset must be saved on disk: " + path)
        item = describe(data)
        item["files"] = files
        assets.append(item)
        touched.update(packages)
    scene = str(request.get("scene", DEFAULT_PROFILE["scene"]))
    if not package_files(scene):
        raise RuntimeError("Capture map does not exist: " + scene)
    yield {"phase": "scene", "completed": len(paths), "total": len(paths)}
    scene_files, scene_packages = yield from dependency_steps(scene, cache)
    if scene == DEFAULT_PROFILE["scene"] and len(scene_packages) <= 1:
        template_files, template_packages = yield from dependency_steps("/Engine/Maps/Templates/Template_Default", cache)
        scene_files = sorted(set(scene_files + template_files))
        scene_packages.update(template_packages)
    scene_package = scene.split(".")[0]
    scene_files = sorted(set(scene_files + package_files(scene_package + "_BuiltData")))
    if {p.split(".")[0] for p in paths}.intersection(scene_packages):
        raise ValueError("A target is also part of the capture environment; use an independent scene")
    unsaved = dirty.intersection(touched | scene_packages)
    if unsaved:
        raise ValueError("Save these packages before Capture: " + ", ".join(sorted(unsaved)))
    return {"assets": assets, "scene_files": scene_files, "scene_package": scene_package, "selection_count": selection_count,
            "engine": str(Path(unreal.Paths.convert_relative_path_to_full(unreal.Paths.engine_dir())).resolve().parent),
            "project": str(project_dir() / (unreal.Paths.get_base_filename(unreal.Paths.get_project_file_path()) + ".uproject")),
            "engine_version": unreal.SystemLibrary.get_engine_version(), "plugin_version": "0.1.0"}


def describe(data):
    return {"path": str(data.object_path), "class": str(data.asset_class), "name": str(data.asset_name)}


def handle(request):
    action = request["action"]
    registry = unreal.AssetRegistryHelpers.get_asset_registry()
    if action == "selection":
        values = unreal.EditorUtilityLibrary.get_selected_asset_data()
        return {"assets": [describe(a) for a in values if str(a.asset_class) in ("StaticMesh", "SkeletalMesh", "Blueprint")]}
    if action == "scan":
        path = str(request.get("path", "/Game"))
        if not path.startswith("/"):
            raise ValueError("Expected an Unreal content directory")
        values = registry.get_assets_by_path(path, recursive=True)
        return {"assets": sorted([describe(a) for a in values if str(a.asset_class) in ("StaticMesh", "SkeletalMesh", "Blueprint")], key=lambda a: a["path"])}
    if action == "prepare":
        steps = prepare_steps(request)
        while True:
            try:
                next(steps)
            except StopIteration as result:
                return result.value
    raise ValueError("Unknown editor operation")


def tick(delta):
    global _busy
    if _busy:
        return
    _busy = True
    try:
        bridge_tick(delta)
    finally:
        _busy = False


def bridge_tick(delta):
    global _last_tick, _preparing, _last_heartbeat
    now = time.monotonic()
    if now - _last_tick < (0.02 if _preparing else 0.25):
        return
    _last_tick = now
    directory = bridge_dir()
    if now - _last_heartbeat >= .25:
        _last_heartbeat = now
        write_json(directory / "editor.json", {"pid": os.getpid(), "session": _session, "heartbeat": time.time(),
            "project": str(project_dir()), "engine_version": unreal.SystemLibrary.get_engine_version()})
    if _preparing:
        path, steps, status = _preparing
        try:
            deadline = time.monotonic() + .01
            while time.monotonic() < deadline:
                if path.with_suffix(".cancel").exists():
                    raise InterruptedError("Capture cancelled during dependency preparation")
                progress = next(steps)
                if progress:
                    status.update(progress)
            write_json(path.with_suffix(".progress"), status)
        except StopIteration as result:
            write_json(directory / "responses" / path.name, {"ok": True, "data": result.value})
            if path.with_suffix(".cancel").exists():
                path.with_suffix(".cancel").unlink()
            _preparing = None
        except Exception as error:
            steps.close()
            write_json(directory / "responses" / path.name, {"ok": False, "error": str(error)})
            if path.with_suffix(".cancel").exists():
                path.with_suffix(".cancel").unlink()
            _preparing = None
        return
    for path in list((directory / "requests").glob("*.json"))[:1]:
        request = read_json(path)
        try:
            if request.get("session") != _session:
                raise ValueError("Editor session has changed; refresh the page")
            if request["action"] == "prepare":
                _preparing = (path, prepare_steps(request), {"phase": "dependencies", "completed": 0, "total": len(request["paths"])})
                path.unlink()
                return
            response = {"ok": True, "data": handle(request)}
        except Exception as error:
            response = {"ok": False, "error": str(error), "traceback": traceback.format_exc()}
        write_json(directory / "responses" / path.name, response)
        path.unlink()


def external_python():
    from .runtime import local_python
    return str(local_python(PLUGIN))


def open_window():
    global _server
    directory = bridge_dir()
    service = read_json(directory / "service.json", {})
    if service.get("url"):
        health = None
        try:
            import urllib.request
            with urllib.request.urlopen(service["url"] + "/api/health", timeout=1) as reply:
                health = json.load(reply)
        except (OSError, ValueError):
            pass
        if health:
            if health.get("application") != "AssetCaptureCompare":
                raise RuntimeError("The saved viewer address belongs to another application")
            if health.get("runtime_revision") == runtime_revision():
                webbrowser.open(service["url"])
                return
            if not service.get("token") or health.get("pid") != service.get("pid"):
                raise RuntimeError("An older viewer is still running (PID {}). Close that Python viewer process and reopen this tool.".format(service.get("pid")))
            request = urllib.request.Request(service["url"] + "/api/shutdown", b"{}",
                {"Content-Type": "application/json", "X-ACC-Token": service["token"]})
            try:
                with urllib.request.urlopen(request, timeout=3) as reply:
                    json.load(reply)
            except urllib.error.HTTPError as error:
                raise RuntimeError(json.load(error).get("error", "Viewer restart was refused")) from error
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                status = read_json(directory / "service.json", {})
                if status.get("pid") == service["pid"] and status.get("stopped"):
                    break
                time.sleep(.1)
            else:
                raise RuntimeError("The old viewer has not finished shutting down; retry shortly")
            _server = None
    if _server is not None and _server.poll() is None:
        return
    output = project_dir() / "Saved/AssetCaptureCompare"
    log = open(str(directory / "service.log"), "ab", buffering=0)
    _server = subprocess.Popen([external_python(), str(PLUGIN / "Scripts/serve.py"), "--root", str(output),
        "--bridge", str(directory)], stdout=log, stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    log.close()
    # The child opens the browser after binding a free localhost port.


def install():
    global _handle
    if _handle is None:
        bridge_dir().mkdir(parents=True, exist_ok=True)
        _handle = unreal.register_slate_post_tick_callback(tick)
    menus = unreal.ToolMenus.get()
    menu = menus.extend_menu("LevelEditor.MainMenu.Window")
    entry = unreal.ToolMenuEntry(name="AssetCaptureCompare.Open", type=unreal.MultiBlockType.MENU_ENTRY)
    entry.set_label("Asset Capture Compare")
    entry.set_tool_tip("Capture selected assets and compare image history")
    entry.set_string_command(unreal.ToolMenuStringCommandType.PYTHON, "", "import acc.editor; acc.editor.open_window()")
    menu.add_menu_entry("AssetCaptureCompare", entry)
    menus.refresh_all_widgets()
    unreal.log("[AssetCaptureCompare] Ready: Window > Asset Capture Compare")
