"""Create the editable Default map in a child editor, preserving the active map."""
import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Content/Python"))
from acc.common import write_json

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", required=True)
    parser.add_argument("--project", required=True)
    args = parser.parse_args()
    job = Path(args.project).parent / "Saved/AssetCaptureCompareBridge/initialize_scene.json"
    write_json(job, {"mode": "initialize_scene"})
    process = subprocess.Popen([str(Path(args.engine) / "Engine/Binaries/Win64/UE4Editor.exe"), args.project,
        "/Engine/Maps/Templates/Template_Default", "-ACCWorker=" + str(job), "-unattended", "-nosplash", "-nosound",
        "-RenderOffscreen", "-NoSourceControl", "-NoRestoreOpenAssetTabs", "-NoAutoSave", "-log=ACCInitialize.log",
        "-ini:Engine:[/Script/PythonScriptPlugin.PythonScriptPluginSettings]:bRemoteExecution=False"],
        creationflags=subprocess.CREATE_NO_WINDOW)
    print("Scene initialization process:", process.pid)
