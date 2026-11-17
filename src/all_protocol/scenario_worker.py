"""Trusted Python compilation in a disposable process. This is not a security sandbox."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback
from dataclasses import asdict

from .application.scenario_model import MAX_MANIFEST_BYTES, MAX_SOURCE_BYTES, decode_rules
from .scenario_sdk import Scene


def worker_main(argv=None):
    source_path, result_path = (argv or sys.argv[1:])[-2:]
    try:
        source = Path(source_path).read_text(encoding="utf-8")
        scene = Scene()
        scope = {"__name__": "__scenario__"}
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            exec(compile(source, "<场景扩展>", "exec"), scope)
            scope["build"](scene)
        result = json.dumps({"rules": [asdict(rule) for rule in scene.rules]}, ensure_ascii=False, allow_nan=False)
        if len(result.encode()) > MAX_MANIFEST_BYTES:
            raise ValueError("执行计划超过 1 MiB")
    except BaseException:
        result = json.dumps({"error": traceback.format_exc(limit=8)}, ensure_ascii=False)
    Path(result_path).write_text(result, encoding="utf-8")
    return 0


def worker_command(source_path, result_path):
    if getattr(sys, "frozen", False):
        return [sys.executable, "--scene-worker", str(source_path), str(result_path)]
    return [sys.executable, "-m", "all_protocol.scenario_worker", str(source_path), str(result_path)]


def compile_source(source, *, timeout=3, cancelled=None):
    if len(source.encode()) > MAX_SOURCE_BYTES:
        raise ValueError("源码超过 256 KiB")
    with tempfile.TemporaryDirectory(prefix="all-protocol-scene-") as directory:
        source_path, result_path = Path(directory) / "scene.py", Path(directory) / "result.json"
        source_path.write_text(source, encoding="utf-8")
        process = subprocess.Popen(worker_command(source_path, result_path), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        import time
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if cancelled is not None and cancelled.is_set():
                    raise ValueError("检查已取消")
                if time.monotonic() >= deadline:
                    raise ValueError(f"检查超时（{timeout:g} 秒），请检查无限循环")
                try:
                    process.wait(timeout=0.03)
                except subprocess.TimeoutExpired:
                    pass
            if not result_path.exists() or result_path.stat().st_size > MAX_MANIFEST_BYTES:
                raise ValueError("检查进程失败或执行计划超过 1 MiB")
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if "error" in result:
                raise ValueError(result["error"])
            return decode_rules(result["rules"])
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


if __name__ == "__main__":
    raise SystemExit(worker_main())
