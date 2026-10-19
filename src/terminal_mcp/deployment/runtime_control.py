"""Durable local maintenance requests, independent of the service being restarted."""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import time
from pathlib import Path

REQUEST_ID = re.compile(r"^[a-zA-Z0-9_-]{16,64}$")
TARGETS = frozenset({"terminal", "tunnel"})
ACTIVE = frozenset({"pending", "running"})


class RuntimeControlError(ValueError):
    pass


@contextlib.contextmanager
def deployment_lock(root: Path):
    """Share the installer's exclusive deployment handle."""
    stream = None
    try:
        if os.name == "nt":
            import msvcrt

            import win32con
            import win32file

            handle = win32file.CreateFile(
                str(root / "deployment.lock"),
                win32con.GENERIC_READ | win32con.GENERIC_WRITE,
                0,
                None,
                win32con.OPEN_ALWAYS,
                0,
                None,
            )
            stream = os.fdopen(msvcrt.open_osfhandle(handle.Detach(), os.O_RDWR), "r+b")
        else:
            import fcntl

            stream = (root / "deployment.lock").open("a+b")
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception as exc:
        if stream is not None:
            stream.close()
        code = "runtime_busy" if getattr(exc, "winerror", 32) in {32, 33} else "runtime_unavailable"
        raise RuntimeControlError(code) from exc
    try:
        yield
    finally:
        stream.close()


def atomic_json(path: Path, value: dict):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".runtime-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "nt":
            from terminal_mcp.platform_windows import secure_deployment_path

            secure_deployment_path(Path(name))
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class RuntimeControl:
    def __init__(self, root: Path, backend, *, clock=time.time):
        self.root, self.backend, self.clock = root, backend, clock
        self.directory = root / "control"

    def _path(self, request_id):
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise RuntimeControlError("invalid_runtime_request")
        return self.directory / "operations" / (request_id + ".json")

    def operation(self, request_id):
        path = self._path(request_id)
        if not path.exists():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if value["status"] in ACTIVE and self.clock() > value["expires_at"]:
            return {**value, "status": "unknown", "error": "runtime_operation_interrupted"}
        return value

    def _current(self):
        pointer = self.directory / "current.json"
        if not pointer.exists():
            return None
        return self.operation(json.loads(pointer.read_text(encoding="utf-8"))["request_id"])

    def snapshot(self):
        return {"ok": True, **self.backend.snapshot(), "operation": self._current()}

    def submit(self, target, request_id):
        path = self._path(request_id)
        if target not in TARGETS:
            raise RuntimeControlError("invalid_runtime_target")
        previous = self.operation(request_id)
        if previous is not None:
            if previous["target"] != target:
                raise RuntimeControlError("runtime_request_conflict")
            return previous
        with deployment_lock(self.root):
            # Re-read after taking the cross-process lock, including concurrent duplicate IDs.
            previous = self.operation(request_id)
            if previous is not None:
                if previous["target"] != target:
                    raise RuntimeControlError("runtime_request_conflict")
                return previous
            if target not in self.backend.snapshot()["restart_targets"]:
                raise RuntimeControlError("runtime_target_unavailable")
            current = self._current()
            if current and current["status"] in ACTIVE:
                raise RuntimeControlError("runtime_busy")
            now = self.clock()
            operation = {
                "request_id": request_id,
                "target": target,
                "status": "pending",
                "created_at": now,
                "expires_at": now + 180,
                "error": None,
            }
            atomic_json(path, operation)
            atomic_json(self.directory / "current.json", {"request_id": request_id})
            try:
                self.backend.launch()
            except Exception:
                operation.update(status="failed", error="runtime_worker_unavailable")
                atomic_json(path, operation)
            return operation

    def run_pending(self):
        with deployment_lock(self.root):
            operation = self._current()
            if not operation or operation["status"] != "pending":
                return
            path = self._path(operation["request_id"])
            operation["status"] = "running"
            atomic_json(path, operation)
            try:
                self.backend.restart(operation["target"])
                operation.update(status="succeeded", error=None)
            except Exception:
                operation.update(status="failed", error="runtime_restart_failed")
            operation["finished_at"] = self.clock()
            atomic_json(path, operation)


def build_runtime_control(settings):
    if os.name == "nt" and settings.windows_service_root is not None:
        from terminal_mcp.deployment.windows_runtime import WindowsRuntime

        return RuntimeControl(settings.windows_service_root, WindowsRuntime(settings))
    return None
