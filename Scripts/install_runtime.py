"""Vendor locked wheels into embedded CPython; no pip or system Python required."""
import argparse
import hashlib
import json
import shutil
import sys
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath

PLUGIN = Path(__file__).resolve().parents[1]


def extract_wheel(archive, destination):
    with zipfile.ZipFile(archive) as wheel:
        for item in wheel.infolist():
            path = PurePosixPath(item.filename)
            if path.is_absolute() or ".." in path.parts or "\\" in item.filename or ":" in item.filename:
                raise ValueError("Unsafe wheel member: " + item.filename)
            if path.parts[0].endswith(".data"):
                if len(path.parts) < 3 or path.parts[1] not in ("purelib", "platlib"):
                    raise ValueError("Unsupported wheel data layout: " + item.filename)
                path = PurePosixPath(*path.parts[2:])
            target = destination.joinpath(*path.parts)
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with wheel.open(item) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)


def install(offline=False):
    runtime = Path(sys.executable).resolve().parent
    if runtime.parent != PLUGIN or not runtime.name.startswith(".runtime-install-"):
        raise RuntimeError("Run Scripts/setup_runtime.ps1 to install into a staging directory")
    lock = json.loads((PLUGIN / "Scripts/runtime-lock.json").read_text(encoding="utf-8"))
    cache = PLUGIN / ".downloads"
    cache.mkdir(exist_ok=True)
    for item in lock["packages"]:
        archive = cache / item["filename"]
        if not archive.exists():
            if offline:
                raise RuntimeError("Missing offline cache: " + str(archive))
            partial = archive.with_suffix("." + uuid.uuid4().hex + ".partial")
            try:
                print("Downloading " + item["filename"], flush=True)
                with urllib.request.urlopen(item["url"], timeout=120) as source, partial.open("wb") as output:
                    shutil.copyfileobj(source, output)
                if hashlib.sha256(partial.read_bytes()).hexdigest() != item["sha256"]:
                    raise RuntimeError("Download checksum mismatch: " + item["filename"])
                partial.replace(archive)
            finally:
                partial.unlink(missing_ok=True)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != item["sha256"]:
            raise RuntimeError("Cached wheel checksum mismatch: " + str(archive))
        extract_wheel(archive, runtime / "Lib/site-packages")
    shutil.copy2(PLUGIN / "Scripts/runtime-lock.json", runtime / "runtime-lock.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true")
    install(parser.parse_args().offline)
