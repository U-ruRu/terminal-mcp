import pytest

from terminal_mcp.fleet.config import FleetConfig
from terminal_mcp.fleet.projection import FleetProjectionService
from terminal_mcp.fleet.projection_storage import FleetProjectionStore
from terminal_mcp.fleet.source import FleetSourceService
from terminal_mcp.fleet.source_meta import FleetNodeMetaStore
from terminal_mcp.storage.events import EventJournalStore
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.terminal.linux import LinuxTerminalAdapter


async def runtime_projection(tmp_path, terminal):
    runtime_path = terminal.repo.path
    meta = FleetNodeMetaStore(
        tmp_path / "fleet-node-meta.sqlite3",
        fleet_id="fleet-a",
        node_id="node-a",
    )
    await meta.initialize()
    source = FleetSourceService(
        runtime_path,
        EventJournalStore(runtime_path),
        meta,
        runtime_health_provider=terminal.health,
    )
    projection = FleetProjectionStore(
        tmp_path / "projection.sqlite3",
        fleet_id="fleet-a",
        node_id="node-a",
        owner_node_id="node-a",
    )
    await projection.initialize()
    service = FleetProjectionService(
        FleetConfig("node-a", "key", (), 1.0, 1.0),
        projection,
        local_source=source,
        owner_node_id="node-a",
    )
    return projection, service, source


@pytest.mark.asyncio
async def test_pidless_running_is_degraded_until_one_durable_repair_event(tmp_path):
    repo = SqliteRepository(tmp_path / "runtime.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    terminal = LinuxTerminalAdapter(
        repo,
        "/bin/bash",
        tmp_path,
        0.1,
        queue_reconcile_sec=0.02,
    )
    command = await repo.create("printf orphan", status="running", queue_id=1)
    projection, service, _source = await runtime_projection(tmp_path, terminal)

    await service.sync_once()
    degraded = await projection.snapshot()
    durable = next(
        item
        for item in degraded["entities"]
        if item["entity_type"] == "command" and item["entity_id"] == command.cmd_hash
    )
    assert durable["payload"]["status"] == "running"
    overlay = degraded["runtime_overlays"][0]["payload"]
    assert overlay["application"] == "terminal-mcp"
    assert overlay["version"]
    assert command.cmd_hash in overlay["stale_running_commands"]
    before_seq = degraded["projection_seq"]

    await terminal.start()
    await service.sync_once()
    repaired = await projection.snapshot()
    durable = next(
        item
        for item in repaired["entities"]
        if item["entity_type"] == "command" and item["entity_id"] == command.cmd_hash
    )
    assert durable["payload"]["status"] == "failed"
    overlay = repaired["runtime_overlays"][0]["payload"]
    assert command.cmd_hash not in overlay["stale_running_commands"]
    assert repaired["projection_seq"] == before_seq + 1
    await terminal.stop()


@pytest.mark.asyncio
async def test_finalization_overlay_loss_never_synthesizes_terminal_truth(tmp_path):
    repo = SqliteRepository(tmp_path / "runtime.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    terminal = LinuxTerminalAdapter(
        repo,
        "/bin/bash",
        tmp_path,
        0.1,
        queue_reconcile_sec=0.02,
    )
    command = await repo.create("printf pending", status="running", queue_id=1)
    terminal.finalization_pending[command.cmd_hash] = {
        "status": "completed",
        "exit_code": 0,
        "error": None,
        "queue_id": 1,
        "attempt": 1,
        "exception_class": "OperationalError",
    }
    projection, service, source = await runtime_projection(tmp_path, terminal)

    await service.sync_once()
    pending = await projection.snapshot()
    before_seq = pending["projection_seq"]
    assert command.cmd_hash in pending["runtime_overlays"][0]["payload"][
        "finalization_pending_commands"
    ]

    restarted_terminal = LinuxTerminalAdapter(
        repo,
        "/bin/bash",
        tmp_path,
        0.1,
        queue_reconcile_sec=0.02,
    )
    restarted_source = FleetSourceService(
        repo.path,
        EventJournalStore(repo.path),
        source.meta_store,
        runtime_health_provider=restarted_terminal.health,
    )
    restarted_service = FleetProjectionService(
        FleetConfig("node-a", "key", (), 1.0, 1.0),
        projection,
        local_source=restarted_source,
        owner_node_id="node-a",
    )
    await restarted_service.sync_once()

    after = await projection.snapshot()
    durable = next(
        item
        for item in after["entities"]
        if item["entity_type"] == "command" and item["entity_id"] == command.cmd_hash
    )
    assert durable["payload"]["status"] == "running"
    assert after["projection_seq"] == before_seq
    assert after["runtime_overlays"][0]["payload"]["finalization_pending_commands"] == []
