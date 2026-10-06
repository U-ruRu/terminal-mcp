import sqlite3
import struct

import pytest

from terminal_mcp.core.service import TerminalService
from terminal_mcp.fleet.control_storage import FleetControlStore
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.terminal.linux import LinuxTerminalAdapter

# Exact 32-byte WAL header preserved from the 2026-10-06 Secondary incident.
INCIDENT_WAL_HEADER = struct.pack(
    ">IIIIIIII",
    0x377F0682,
    3_007_000,
    4096,
    12,
    0xC0E2A963,
    0x833FD399,
    0x3890E5D3,
    0x8A4BFCFB,
)


def overwrite_main_header_with_wal_prefix(path):
    original_size = path.stat().st_size
    with path.open("r+b") as handle:
        original_header = handle.read(32)
        assert original_header.startswith(b"SQLite format 3\x00")
        handle.seek(0)
        handle.write(INCIDENT_WAL_HEADER)
        handle.flush()
    assert path.stat().st_size == original_size
    assert path.read_bytes()[:32] == INCIDENT_WAL_HEADER
    return original_header


@pytest.mark.asyncio
async def test_fleet_control_incident_shape_is_a_32_byte_main_header_overwrite(tmp_path):
    path = tmp_path / "fleet-control.sqlite3"
    store = FleetControlStore(
        path,
        fleet_id="fleet-a",
        node_id="secondary",
        control_node_id="main",
    )
    await store.initialize()
    assert await store.schema_version() == 3

    original_header = overwrite_main_header_with_wal_prefix(path)

    with pytest.raises(sqlite3.DatabaseError, match="file is not a database"):
        await store.schema_version()

    # Forensic repair on the test copy: restoring only the original 32-byte
    # SQLite header is sufficient to make the untouched database pages valid.
    with path.open("r+b") as handle:
        handle.write(original_header)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert await store.schema_version() == 3


@pytest.mark.asyncio
async def test_runtime_health_does_not_detect_corrupted_fleet_control_sidecar(tmp_path):
    runtime_path = tmp_path / "runtime.sqlite3"
    repo = SqliteRepository(runtime_path)
    await repo.initialize()
    terminal = LinuxTerminalAdapter(repo, "/bin/bash", tmp_path, 0.1)
    service = TerminalService(repo, terminal, 5000)

    control = FleetControlStore(
        tmp_path / "fleet-control.sqlite3",
        fleet_id="fleet-a",
        node_id="secondary",
        control_node_id="main",
    )
    await control.initialize()
    overwrite_main_header_with_wal_prefix(control.path)

    await terminal.start()
    try:
        health = await service.health("none")
        assert health["storage"] == "ok"
        with pytest.raises(sqlite3.DatabaseError, match="file is not a database"):
            await control.schema_version()
    finally:
        await terminal.stop()
