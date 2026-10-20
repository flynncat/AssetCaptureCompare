"""Development command transport using Epic's local Python remote protocol."""
import argparse
import importlib.util
import json
import socket
import time
import types
from pathlib import Path


def execute(engine, command):
    module_path = Path(engine) / "Engine/Plugins/Experimental/PythonScriptPlugin/Content/Python/remote_execution.py"
    spec = importlib.util.spec_from_file_location("epic_remote", str(module_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._json = types.SimpleNamespace(dumps=json.dumps, loads=lambda value, **kw: json.loads(value))
    config = module.RemoteExecutionConfig()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        config.command_endpoint = ("127.0.0.1", sock.getsockname()[1])
    remote = module.RemoteExecution(config)
    remote.start()
    try:
        deadline = time.monotonic() + 8
        while not remote.remote_nodes and time.monotonic() < deadline:
            time.sleep(0.2)
        engine_root = (Path(engine) / "Engine").resolve()
        nodes = [n for n in remote.remote_nodes if Path(n.get("engine_root", "")).resolve() == engine_root]
        if len(nodes) != 1:
            raise RuntimeError("Expected one editor; discovered: {}".format(nodes))
        remote.open_command_connection(nodes[0]["node_id"])
        result = remote.run_command(command, raise_on_failure=False)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result["success"]:
            raise SystemExit(1)
    finally:
        remote.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--command")
    source.add_argument("--file")
    args = parser.parse_args()
    command = args.command
    if args.file:
        command = "exec(compile(open({0!r}, encoding='utf-8').read(), {0!r}, 'exec'))".format(str(Path(args.file).resolve()))
    execute(args.engine, command)
