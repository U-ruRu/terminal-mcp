"""Linux-only Fleet-control writer attribution for scrubbed sandbox copies.

This module deliberately refuses non-temporary targets. It exists to attribute a
raw filesystem writer during controlled reproduction without exposing command
arguments or mutating production evidence.
"""

from __future__ import annotations

import ctypes
import os
import select
import struct
import subprocess
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

# Header inspection is only permitted on sandbox copies, never live databases.
SQLITE_MAIN_HEADER = b"SQLite format 3\x00"
SQLITE_WAL_MAGICS = {bytes.fromhex("377f0682"), bytes.fromhex("377f0683")}

FAN_CLOEXEC = 0x00000001
FAN_NONBLOCK = 0x00000002
FAN_CLASS_NOTIF = 0x00000000
FAN_MODIFY = 0x00000002
FAN_CLOSE_WRITE = 0x00000008
FAN_MARK_ADD = 0x00000001
AT_FDCWD = -100
FANOTIFY_METADATA_VERSION = 3
_METADATA = struct.Struct("=IBBHQii")
_TEMP_ROOTS = (Path("/tmp"), Path("/var/tmp"))


class WriterTraceUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class WriterEvent:
    pid: int
    comm: str
    executable: str
    relative_path: str
    mask: int
    event: str
    header_kind: str
    header_hex: str

    def public_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TraceResult:
    returncode: int
    initial_header_kind: str
    final_header_kind: str
    events: tuple[WriterEvent, ...]

    def public_dict(self) -> dict:
        return {
            "returncode": self.returncode,
            "initial_header_kind": self.initial_header_kind,
            "final_header_kind": self.final_header_kind,
            "events": [event.public_dict() for event in self.events],
        }


def classify_header(header: bytes) -> str:
    if header.startswith(SQLITE_MAIN_HEADER):
        return "sqlite_main"
    if header[:4] in SQLITE_WAL_MAGICS:
        return "wal_header"
    return "unknown"


def validate_sandbox_target(sandbox_root: Path, target: Path) -> tuple[Path, Path]:
    root = sandbox_root.resolve(strict=True)
    target = target.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("sandbox root must be a directory")
    if not any(root == allowed or root.is_relative_to(allowed) for allowed in _TEMP_ROOTS):
        raise ValueError("writer tracing is restricted to /tmp or /var/tmp sandboxes")
    if target.is_symlink() or not target.is_file():
        raise ValueError("target must be a regular non-symlink file")
    if target.name != "fleet-control.sqlite3":
        raise ValueError("target must be named fleet-control.sqlite3")
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("target must be contained by sandbox root") from exc
    return root, target


def _process_identity(pid: int) -> tuple[str, str]:
    # Deliberately omit argv/cmdline: pairing links, tokens and other secrets may
    # appear there. comm + executable is sufficient for writer attribution.
    proc = Path("/proc") / str(pid)
    try:
        comm = (proc / "comm").read_text(encoding="utf-8").strip()
    except OSError:
        comm = "exited"
    try:
        executable = os.readlink(proc / "exe")
    except OSError:
        executable = "exited"
    return comm[:128], executable[:1024]


def _event_name(mask: int) -> str:
    parts = []
    if mask & FAN_MODIFY:
        parts.append("modify")
    if mask & FAN_CLOSE_WRITE:
        parts.append("close_write")
    return "+".join(parts) or f"mask_{mask:x}"


class FanotifyWatcher:
    def __init__(self, sandbox_root: Path, target: Path):
        self.root, self.target = validate_sandbox_target(sandbox_root, target)
        self._libc = ctypes.CDLL(None, use_errno=True)
        self._libc.fanotify_init.argtypes = [ctypes.c_uint, ctypes.c_uint]
        self._libc.fanotify_init.restype = ctypes.c_int
        self._libc.fanotify_mark.argtypes = [
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_ulonglong,
            ctypes.c_int,
            ctypes.c_char_p,
        ]
        self._libc.fanotify_mark.restype = ctypes.c_int
        self.fd = self._libc.fanotify_init(
            FAN_CLASS_NOTIF | FAN_CLOEXEC | FAN_NONBLOCK,
            os.O_RDONLY | os.O_LARGEFILE,
        )
        if self.fd < 0:
            errno = ctypes.get_errno()
            raise WriterTraceUnavailable(os.strerror(errno))
        rc = self._libc.fanotify_mark(
            self.fd,
            FAN_MARK_ADD,
            FAN_MODIFY | FAN_CLOSE_WRITE,
            AT_FDCWD,
            os.fsencode(self.target),
        )
        if rc < 0:
            errno = ctypes.get_errno()
            os.close(self.fd)
            self.fd = -1
            raise WriterTraceUnavailable(os.strerror(errno))

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> FanotifyWatcher:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def read_events(self, timeout_seconds: float) -> list[WriterEvent]:
        poller = select.poll()
        poller.register(self.fd, select.POLLIN)
        if not poller.poll(max(0, int(timeout_seconds * 1000))):
            return []
        data = os.read(self.fd, 64 * 1024)
        events: list[WriterEvent] = []
        offset = 0
        while offset + _METADATA.size <= len(data):
            event_len, version, _, metadata_len, mask, event_fd, pid = _METADATA.unpack_from(
                data, offset
            )
            if event_len < _METADATA.size or metadata_len < _METADATA.size:
                break
            if version != FANOTIFY_METADATA_VERSION:
                raise WriterTraceUnavailable(
                    f"unsupported fanotify metadata version: {version}"
                )
            try:
                if event_fd < 0:
                    continue
                path = Path(os.readlink(f"/proc/self/fd/{event_fd}")).resolve()
                try:
                    relative = str(path.relative_to(self.root))
                except ValueError:
                    relative = "<outside-sandbox>"
                comm, executable = _process_identity(pid)
                header = self.target.read_bytes()[:32]
                events.append(
                    WriterEvent(
                        pid=pid,
                        comm=comm,
                        executable=executable,
                        relative_path=relative,
                        mask=mask,
                        event=_event_name(mask),
                        header_kind=classify_header(header),
                        header_hex=header.hex(),
                    )
                )
            finally:
                if event_fd >= 0:
                    os.close(event_fd)
            offset += event_len
        return events


def trace_command(
    sandbox_root: Path,
    target: Path,
    command: Sequence[str],
    *,
    timeout_seconds: float = 10.0,
) -> TraceResult:
    if not command:
        raise ValueError("command is required")
    root, target = validate_sandbox_target(sandbox_root, target)
    initial_kind = classify_header(target.read_bytes()[:32])
    if initial_kind != "sqlite_main":
        raise ValueError("target must start as a valid SQLite main file")

    events: list[WriterEvent] = []
    with FanotifyWatcher(root, target) as watcher:
        child = subprocess.Popen(tuple(command), cwd=root)
        deadline = time.monotonic() + timeout_seconds
        idle_after_exit = 0
        while time.monotonic() < deadline:
            batch = watcher.read_events(0.1)
            events.extend(batch)
            if child.poll() is None:
                continue
            if batch:
                idle_after_exit = 0
                continue
            idle_after_exit += 1
            if idle_after_exit >= 2:
                break
        if child.poll() is None:
            child.kill()
            child.wait()
            raise TimeoutError("traced command exceeded timeout")
        returncode = int(child.returncode)

    final_kind = classify_header(target.read_bytes()[:32])
    return TraceResult(
        returncode=returncode,
        initial_header_kind=initial_kind,
        final_header_kind=final_kind,
        events=tuple(events),
    )
