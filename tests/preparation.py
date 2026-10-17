"""Reusable empty/immutable preparation, never shared application state."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from collections import OrderedDict
from collections.abc import Callable
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from threading import RLock
from typing import TypeVar

T = TypeVar("T")


class ImmutablePreparationCache:
    """Run the real generator once per key; every consumer receives a deep copy."""

    def __init__(self, max_entries: int = 128):
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self.max_entries = max_entries
        self._values: OrderedDict[object, object] = OrderedDict()
        self._lock = RLock()
        self.hits = 0
        self.misses = 0

    def get(self, key: object, build: Callable[[], T]) -> T:
        try:
            hash(key)
        except TypeError:
            return build()
        with self._lock:
            if key not in self._values:
                self._values[key] = deepcopy(build())
                self.misses += 1
                if len(self._values) > self.max_entries:
                    self._values.popitem(last=False)
            else:
                self.hits += 1
            self._values.move_to_end(key)
            return deepcopy(self._values[key])

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"hits": self.hits, "misses": self.misses, "entries": len(self._values)}


def snapshot_empty_database(source: Path, destination: Path, expected_version: int) -> bytes:
    """SQLite's backup API includes committed WAL data; raw live DB copies are forbidden."""
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
        assert original.execute("PRAGMA user_version").fetchone()[0] == expected_version
        tables = [
            r[0]
            for r in original.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for table in tables:
            escaped = table.replace('"', '""')
            assert original.execute(f'SELECT count(*) FROM "{escaped}"').fetchone()[0] == 0, (
                f"Fixture schema contains rows in {table}; review preparation before reuse"
            )
        with closing(sqlite3.connect(destination)) as copy:
            original.backup(copy)
            assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    return destination.read_bytes()


def seed_absent_database(destination: Path, contents: bytes, sandbox: Path) -> bool:
    """Atomically seed only absent files under this pytest run's private temp root."""
    destination = Path(destination)
    if not destination.resolve().is_relative_to(sandbox.resolve()):
        return False
    if os.path.lexists(destination):
        return False
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".empty-schema-", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(contents)
        try:
            # Hard-link publication is atomic and refuses to overwrite a concurrent creator.
            os.link(temporary, destination)
        except FileExistsError:
            return False
        return True
    finally:
        os.unlink(temporary)


def immutable_schema_models() -> tuple[type, ...]:
    """Explicit static catalogs only; never monkeypatch BaseModel or runtime validation."""
    from pydantic import RootModel

    from terminal_mcp.mcp.role_contracts import ROLE_TOOL_MODELS
    from terminal_mcp.mcp.role_outputs import ROLE_OUTPUT_MODELS, RoleFailure

    wire = [
        RootModel[model.__success_type__ | RoleFailure] for model in ROLE_OUTPUT_MODELS.values()
    ]
    return tuple(dict.fromkeys([*ROLE_TOOL_MODELS.values(), *wire]))
