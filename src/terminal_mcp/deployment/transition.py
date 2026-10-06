"""Crash-recoverable topology file transaction with explicit service ordering.

The caller supplies the read-only preflight and real readiness probes. This is
not a database rollback: only three small service/config files are journaled.
A pending command or unreadable store blocks rollback rather than risking replay.
"""

from __future__ import annotations

import base64
import fcntl
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from pathlib import Path

from terminal_mcp.deployment.split import API_SERVICE, EXECUTOR_SERVICE, CutoverError

MAX_FILE_BYTES = 128 * 1024
MAX_JOURNAL_BYTES = 1024 * 1024


def _regular_target(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise CutoverError("absolute_target_required")
    if any(parent.is_symlink() for parent in [path, *path.parents]):
        raise CutoverError("symlink_target_refused")
    if path.exists() and not path.is_file():
        raise CutoverError("non_regular_target_refused")


def _atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
    _regular_target(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".cutover-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            os.fchmod(output.fileno(), mode)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class TopologyTransition:
    def __init__(
        self,
        files: Mapping[Path, str],
        journal: Path,
        *,
        control: Callable[..., None],
        check: Callable[[], object],
        ready: Callable[[str], None],
        identity: str | None = None,
    ):
        if len(files) != 3:
            raise CutoverError("exactly_three_topology_files_required")
        self.files = dict(files)
        self.identity = identity
        self.journal = journal
        self.control, self.check, self.ready = control, check, ready
        _regular_target(journal)
        if journal in files:
            raise CutoverError("journal_target_collision")
        for path, text in self.files.items():
            _regular_target(path)
            if len(text.encode()) > MAX_FILE_BYTES:
                raise CutoverError("topology_file_too_large")

    @contextmanager
    def _lock(self):
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        path = self.journal.with_suffix(self.journal.suffix + ".lock")
        _regular_target(path)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise CutoverError("cutover_in_progress") from exc
            yield
        finally:
            os.close(fd)

    def _save(self, record: dict, phase: str) -> None:
        record["phase"] = phase
        encoded = json.dumps(record, sort_keys=True).encode()
        if len(encoded) > MAX_JOURNAL_BYTES:
            raise CutoverError("journal_too_large")
        _atomic(self.journal, encoded)

    def _load(self) -> dict:
        _regular_target(self.journal)
        if self.journal.stat().st_size > MAX_JOURNAL_BYTES:
            raise CutoverError("journal_too_large")
        stat = self.journal.stat()
        if stat.st_uid != os.geteuid() or stat.st_mode & 0o022:
            raise CutoverError("unsafe_journal_ownership")
        record = json.loads(self.journal.read_text())
        if record.get("identity") != self.identity:
            raise CutoverError("release_identity_mismatch")
        if record.get("version") != 1 or set(record.get("before", {})) != {
            str(path) for path in self.files
        }:
            raise CutoverError("journal_target_mismatch")
        if record.get("phase") not in {
            "prepared",
            "files_installed",
            "executor_ready",
            "active",
            "failed",
            "rollback_blocked",
            "rolled_back",
        }:
            raise CutoverError("invalid_journal_phase")
        # Validate the entire snapshot before stopping services or restoring any file.
        for previous in record["before"].values():
            if previous is None:
                continue
            if not isinstance(previous, dict) or set(previous) != {"data", "mode"}:
                raise CutoverError("invalid_journal_snapshot")
            mode = previous["mode"]
            if type(mode) is not int or not 0 <= mode <= 0o777:
                raise CutoverError("invalid_journal_permissions")
            try:
                data = base64.b64decode(previous["data"], validate=True)
            except (ValueError, TypeError) as exc:
                raise CutoverError("invalid_journal_data") from exc
            if len(data) > MAX_FILE_BYTES:
                raise CutoverError("existing_file_too_large")
        return record

    def _snapshot(self) -> dict:
        before = {}
        for path in self.files:
            _regular_target(path)
            if path.exists():
                if path.stat().st_size > MAX_FILE_BYTES:
                    raise CutoverError("existing_file_too_large")
                before[str(path)] = {
                    "data": base64.b64encode(path.read_bytes()).decode(),
                    "mode": path.stat().st_mode & 0o777,
                }
            else:
                before[str(path)] = None
        return {"version": 1, "before": before, "identity": self.identity}

    def _restore(self, record: dict) -> None:
        self.control("stop", API_SERVICE)
        # Read again with admission stopped; never stop the executor with live work.
        self.check()
        self.control("stop", EXECUTOR_SERVICE)
        for path in self.files:
            _regular_target(path)
            previous = record["before"][str(path)]
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                data = base64.b64decode(previous["data"], validate=True)
                if len(data) > MAX_FILE_BYTES:
                    raise CutoverError("existing_file_too_large")
                _atomic(path, data, previous["mode"])
        self.control("daemon-reload")
        self.control("start", API_SERVICE)
        self.ready(API_SERVICE)
        self._save(record, "rolled_back")

    def activate(self) -> None:
        with self._lock():
            if self.journal.exists():
                old = self._load()
                if old["phase"] == "active" and all(
                    path.is_file() and path.read_text() == text for path, text in self.files.items()
                ):
                    return  # No gratuitous restart on an idempotent invocation.
                if old["phase"] != "rolled_back":
                    raise CutoverError("unfinished_cutover_requires_rollback")
            self.check()
            record = self._snapshot()
            self._save(record, "prepared")
            try:
                self.control("stop", API_SERVICE)
                self.check()
                for path, text in self.files.items():
                    _atomic(path, text.encode(), 0o644)
                self._save(record, "files_installed")
                self.control("daemon-reload")
                self.control("start", EXECUTOR_SERVICE)
                self.ready(EXECUTOR_SERVICE)
                self._save(record, "executor_ready")
                self.control("start", API_SERVICE)
                self.ready(API_SERVICE)
                self._save(record, "active")
            except Exception as original:
                self._save(record, "failed")
                try:
                    self._restore(record)
                except Exception as rollback_error:
                    self._save(record, "rollback_blocked")
                    raise CutoverError(
                        "rollback_blocked_preserve_executor_and_stores"
                    ) from rollback_error
                raise original

    def rollback(self) -> None:
        with self._lock():
            record = self._load()
            if record["phase"] == "rolled_back":
                return
            self.check()
            try:
                self._restore(record)
            except Exception as exc:
                self._save(record, "rollback_blocked")
                raise CutoverError("rollback_blocked_preserve_executor_and_stores") from exc
