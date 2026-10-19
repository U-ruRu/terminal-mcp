"""Small, secret-safe primitives shared by release packaging and deployment."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
MAX_JSON = 4 * 1024 * 1024


class ReleaseError(RuntimeError):
    """Only a stable diagnostic code is printable; subprocess output stays private."""


def require(value: object, code: str) -> None:
    if not value:
        raise ReleaseError(code)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def encoded(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def read_json(path: Path) -> dict:
    require(path.is_file() and path.stat().st_size <= MAX_JSON, "json_missing_or_too_large")
    try:
        value = json.loads(path.read_bytes())
    except (ValueError, UnicodeError) as exc:
        raise ReleaseError("invalid_json") from exc
    require(isinstance(value, dict), "json_object_required")
    return value


def safe_path(path: Path, *, owner: bool = False) -> Path:
    require(path.is_absolute() and ".." not in path.parts, "absolute_path_required")
    require(not any(p.is_symlink() for p in (path, *path.parents)), "symlink_path_refused")
    if path.exists() and owner:
        info = path.stat()
        require(info.st_uid == os.geteuid() and not info.st_mode & 0o022, "unsafe_path_ownership")
    return path


def sync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".release-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            if os.name != "nt":
                os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "nt":
            from terminal_mcp.platform_windows import secure_deployment_path

            secure_deployment_path(Path(name))
        os.replace(name, path)
        sync_dir(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def run(
    argv: list[str | Path],
    *,
    timeout: float = 60,
    env: dict | None = None,
    cwd: Path | None = None,
    code: str = "subprocess_failed",
) -> str:
    """Never echo commands, environment, stdout or stderr in an exception/log."""
    try:
        p = subprocess.run(
            [str(x) for x in argv],
            check=False,
            capture_output=True,
            timeout=timeout,
            env=env,
            cwd=cwd,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise ReleaseError(code + "_outcome_unknown") from exc
    except OSError as exc:
        raise ReleaseError(code + "_unavailable") from exc
    require(p.returncode == 0, code)
    return p.stdout


def readonly(path: Path) -> sqlite3.Connection:
    require(path.is_file(), "required_store_missing")
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    db.execute("PRAGMA query_only=ON")
    return db


def schema_versions(stores: dict[str, str]) -> dict[str, int]:
    result = {}
    for name in ("runtime", "auth", "control"):
        if name not in stores or not Path(stores[name]).exists():
            continue
        db = readonly(Path(stores[name]))
        try:
            if name == "control":
                row = db.execute("SELECT version FROM control_schema WHERE singleton=1").fetchone()
                require(row is not None, "control_schema_marker_missing")
                result[name] = int(row[0])
            else:
                result[name] = int(db.execute("PRAGMA user_version").fetchone()[0])
        except sqlite3.Error as exc:
            raise ReleaseError("store_schema_unreadable") from exc
        finally:
            db.close()
    return result


def utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
