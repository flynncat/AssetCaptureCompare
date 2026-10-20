"""Exercise standalone installation, real image comparison and external asset edits."""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Content/Python"))
from acc.common import DEFAULT_PROFILE, PLUGIN, read_json, write_json
from acc.storage import Store
from acc.scheduler import Scheduler

shutdown_returncodes = []

def editor_script(engine, project, script):
    log = project.parent / "Saved" / (script.stem + ".log")
    result = subprocess.run([str(engine / "Engine/Binaries/Win64/UE4Editor.exe"), str(project),
        "/Engine/Maps/Templates/Template_Default", "-ExecutePythonScript=" + str(script), "-RenderOffscreen", "-d3d11",
        "-unattended", "-nosplash", "-nosound", "-NoSourceControl", "-NoAutoSave", "-abslog=" + str(log),
        "-ini:Engine:[/Script/PythonScriptPlugin.PythonScriptPluginSettings]:bRemoteExecution=False"],
        timeout=300, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    shutdown_returncodes.append(result.returncode)
    if result.returncode:
        raise RuntimeError("Probe editor failed; inspect " + str(log))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--parent", type=Path, required=True)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--installed", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    root = args.parent / "ACCCleanAcceptance"
    root.mkdir(parents=True, exist_ok=True)
    project = root / "ACCCleanAcceptance.uproject"
    if not project.exists():
        write_json(project, {"FileVersion": 3, "EngineAssociation": "", "DisableEnginePluginsByDefault": True,
            "Plugins": [{"Name": "AssetCaptureCompare", "Enabled": True}]})
    if not args.installed:
        destination = root / "Plugins/AssetCaptureCompare"
        if destination.resolve() == PLUGIN:
            raise RuntimeError("Acceptance destination must differ from the source plugin")
        for name in ("AssetCaptureCompare.uplugin", "Content", "Source", "Binaries", "Web", "Scripts", "Tools", "Tests", ".runtime"):
            source = PLUGIN / name
            if source.is_dir():
                shutil.copytree(source, destination / name, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns("__pycache__", "ACCWork", "artifacts"))
            else:
                destination.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination / name)
        interpreter = destination / ".runtime/python.exe"
        subprocess.run([str(interpreter), str(destination / "Scripts/verify_runtime.py")], check=True)
        command = [str(interpreter), str(destination / "Tools/clean_project_smoke.py"),
                   "--engine", str(args.engine), "--parent", str(args.parent), "--installed"]
        if args.prepare_only:
            command.append("--prepare-only")
        sys.exit(subprocess.run(command).returncode)
    assert PLUGIN == root / "Plugins/AssetCaptureCompare", "Test must run entirely from the installed copy"
    editor_script(args.engine, project, PLUGIN / "Tests/prepare_probe_unreal.py")
    print("Independent clean-project setup complete", flush=True)
    if args.prepare_only:
        sys.exit(0)
    prepared = read_json(root / "Saved/prepared.json")
    prepared["assets"][0]["files"] = sorted(str(p) for p in (root / "Content/ACCProbe").glob("*.uasset"))
    store = Store(root / "Saved/AssetCaptureCompare")
    scheduler = Scheduler(store)
    results = []
    for label in ("Probe original", "Probe unchanged", "Probe changed material", "Probe enlarged geometry"):
        if label == "Probe changed material":
            editor_script(args.engine, project, PLUGIN / "Tests/change_probe_unreal.py")
        if label == "Probe enlarged geometry":
            editor_script(args.engine, project, PLUGIN / "Tests/grow_probe_unreal.py")
        batch = scheduler.start(prepared, dict(DEFAULT_PROFILE), 1, label)
        scheduler.thread.join()
        batch = store.batch(batch["batch_id"])
        if batch["status"] != "success":
            raise RuntimeError("Probe capture failed: " + json.dumps(batch["assets"]))
        capture = store.capture(batch["assets"][0]["capture_id"])
        assert capture["live_capture_worlds_after_cleanup"] == 0, capture
        results.append(batch)
        print(label + ": " + batch["batch_id"], flush=True)
    same = results[1]["assets"][0]["comparison"]
    changed = results[2]["assets"][0]["comparison"]
    assert same["status"] == "success" and same["score"] >= .995, same
    assert changed["status"] == "success" and changed["score"] < .97, changed
    enlarged = results[3]["assets"][0]["comparison"]
    assert enlarged["status"] == "incompatible" and "framing_id" in enlarged["reasons"], enlarged
    report = {"status": "success", "unchanged": same, "material_change": changed, "enlarged_geometry": enlarged,
              "batches": [b["batch_id"] for b in results], "project": str(project), "editor_shutdown_returncodes": shutdown_returncodes}
    write_json(root / "Saved/acceptance.json", report)
    print(json.dumps(report), flush=True)
    scheduler.processing.shutdown(wait=True)
