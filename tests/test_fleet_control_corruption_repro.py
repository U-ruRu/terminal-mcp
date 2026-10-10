import asyncio
import sqlite3
import struct

import aiosqlite
import pytest

from terminal_mcp.core.service import TerminalService
from terminal_mcp.fleet.control_storage import FleetControlError, FleetControlStore
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

    corrupted = path.read_bytes()
    with pytest.raises(FleetControlError, match="fleet_control_invalid_header"):
        await store.schema_version()
    assert await store.healthy() is False
    assert path.read_bytes() == corrupted

    # Forensic repair on the test copy: restoring only the original 32-byte
    # SQLite header is sufficient to make the untouched database pages valid.
    with path.open("r+b") as handle:
        handle.write(original_header)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert await store.schema_version() == 3


@pytest.mark.asyncio
async def test_runtime_health_detects_corrupted_fleet_control_main_file(tmp_path):
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
    service.fleet_control = control

    await terminal.start()
    try:
        health = await service.health("none")
        assert health["storage"] == "error"
        assert health["ok"] is False
        with pytest.raises(FleetControlError, match="fleet_control_invalid_header"):
            await control.schema_version()
    finally:
        await terminal.stop()


def managed_snapshot(revision: int) -> dict:
    return {
        "managed": True,
        "fleet_id": "fleet-a",
        "control_node_id": "main",
        "meshes": [{"mesh_id": "mesh-a", "display_name": "Alpha", "adopted": True}],
        "nodes": [
            {
                "node_id": "main",
                "origin": "https://main.example",
                "public_key": "pub-main",
                "mesh_id": "mesh-a",
                "state": "active",
                "applied_topology_revision": revision,
                "applied_trust_revision": revision,
                "applied_policy_revision": revision,
            },
            {
                "node_id": "secondary",
                "origin": "https://secondary.example",
                "public_key": "pub-secondary",
                "mesh_id": "mesh-a",
                "state": "active",
                "applied_topology_revision": revision,
                "applied_trust_revision": revision,
                "applied_policy_revision": revision,
            },
        ],
        "policy": {
            "duration_seconds": 1380,
            "warning_after_seconds": 1200,
            "alert_after_seconds": 1320,
            "rearm_after_seconds": 120,
            "legacy_admission_enabled": False,
        },
        "revisions": {
            "routing": revision,
            "topology": revision,
            "trust": revision,
            "access_policy": revision,
        },
    }


@pytest.mark.asyncio
async def test_wal_concurrency_never_replaces_fleet_control_main_header(tmp_path):
    path = tmp_path / "fleet-control.sqlite3"
    store = FleetControlStore(
        path,
        fleet_id="fleet-a",
        node_id="secondary",
        control_node_id="main",
    )
    await store.initialize()
    stop = asyncio.Event()
    errors: list[Exception] = []

    def assert_main_header():
        assert path.read_bytes()[:16] == b"SQLite format 3\x00"

    async def writer():
        for revision in range(1, 31):
            await store.apply_managed_replica(
                managed_snapshot(revision),
                bootstrap_tokens={"main": "tok-main", "secondary": "tok-secondary"},
            )
            assert_main_header()
        stop.set()

    async def reader():
        while not stop.is_set():
            try:
                state = await store.control_state()
                assert state["schema_version"] == 3
                assert_main_header()
            except Exception as exc:  # pragma: no cover - asserted after gather
                errors.append(exc)
            await asyncio.sleep(0)

    async def reopen():
        while not stop.is_set():
            try:
                other = FleetControlStore(
                    path,
                    fleet_id="fleet-a",
                    node_id="secondary",
                    control_node_id="main",
                )
                assert await other.schema_version() == 3
                assert_main_header()
            except Exception as exc:  # pragma: no cover - asserted after gather
                errors.append(exc)
            await asyncio.sleep(0)

    async def checkpoint():
        while not stop.is_set():
            try:
                async with aiosqlite.connect(path, timeout=0.05) as db:
                    await db.execute("PRAGMA busy_timeout=50")
                    await db.execute("PRAGMA wal_checkpoint(PASSIVE)")
                    await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                assert_main_header()
            except sqlite3.Error:
                pass
            await asyncio.sleep(0)

    workers = [
        asyncio.create_task(reader()),
        asyncio.create_task(reader()),
        asyncio.create_task(reopen()),
        asyncio.create_task(checkpoint()),
    ]
    await writer()
    await asyncio.gather(*workers)
    assert not errors
    assert_main_header()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        revisions = db.execute(
            "SELECT topology_revision,trust_revision,access_policy_revision "
            "FROM control_meta WHERE singleton=1"
        ).fetchone()
    assert revisions == (30, 30, 30)
