from __future__ import annotations

import os
from pathlib import Path

from terminal_mcp.auth.foundation import AuthFoundationError

_MAGIC = b"TMC-ACCESS-KEY-v1\x00"
_KEY_BYTES = 32


class AccessVerifierKeyStore:
    """Root-local durable key material for Access-code verification.

    The key is intentionally independent from transport/session/Fleet credentials so
    unrelated key rotation cannot invalidate Access codes. The file is colocated
    with the rollback-excluded auth database and must survive ordinary releases.
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    def _read(self) -> bytes:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError as exc:
            raise AuthFoundationError("access verifier key is missing") from exc
        expected = len(_MAGIC) + _KEY_BYTES
        if len(raw) != expected or not raw.startswith(_MAGIC):
            raise AuthFoundationError("access verifier key is corrupt")
        try:
            mode = self.path.stat().st_mode & 0o777
        except OSError as exc:
            raise AuthFoundationError("access verifier key is unreadable") from exc
        if mode & 0o077:
            raise AuthFoundationError("access verifier key permissions are insecure")
        return raw[len(_MAGIC) :]

    def load_or_create(self, *, allow_create: bool) -> bytes:
        if self.path.exists():
            return self._read()
        if not allow_create:
            raise AuthFoundationError("access verifier key is missing for existing Access state")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        key = os.urandom(_KEY_BYTES)
        payload = _MAGIC + key
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return self._read()
        try:
            with os.fdopen(fd, "wb", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            try:
                self.path.unlink(missing_ok=True)
            finally:
                raise
        return self._read()
