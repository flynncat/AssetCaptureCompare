import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "Content/Python"))
sys.path.insert(0, str(PLUGIN / "Scripts"))
from acc.runtime import local_python
from install_runtime import extract_wheel


class RuntimeTests(unittest.TestCase):
    def test_global_python_and_old_config_are_not_used(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin = Path(directory)
            (plugin / "Scripts").mkdir()
            (plugin / "Scripts/runtime-lock.json").write_text('{}')
            (plugin / "Config").mkdir()
            (plugin / "Config/runtime.json").write_text(json.dumps({"python": sys.executable}))
            with patch.dict(os.environ, {"ACC_PYTHON": sys.executable}):
                with self.assertRaisesRegex(RuntimeError, "setup_runtime"):
                    local_python(plugin)

    def test_local_runtime_requires_matching_lock_and_rejects_venv(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin = Path(directory)
            (plugin / "Scripts").mkdir()
            (plugin / ".runtime").mkdir()
            (plugin / "Scripts/runtime-lock.json").write_text('{"schema": 1}')
            (plugin / ".runtime/python.exe").write_bytes(b"fixture")
            manifest = plugin / ".runtime/runtime-lock.json"
            manifest.write_text('{}')
            with self.assertRaises(RuntimeError):
                local_python(plugin)
            manifest.write_text('{"schema": 1}')
            self.assertEqual(local_python(plugin), plugin / ".runtime/python.exe")
            (plugin / ".runtime/pyvenv.cfg").write_text('home = external')
            with self.assertRaises(RuntimeError):
                local_python(plugin)

    def test_wheel_members_cannot_escape_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('../outside.py', '/absolute.py', 'C:/outside.py', '..\\outside.py'):
                archive = root / "fixture.whl"
                with zipfile.ZipFile(archive, "w") as wheel:
                    wheel.writestr(name, b"fixture")
                with self.assertRaises(ValueError):
                    extract_wheel(archive, root / "site-packages")

    def test_purelib_data_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "fixture.whl"
            with zipfile.ZipFile(archive, "w") as wheel:
                wheel.writestr("fixture.data/purelib/example.py", b"fixture")
            extract_wheel(archive, root / "site-packages")
            self.assertEqual((root / "site-packages/example.py").read_bytes(), b"fixture")
