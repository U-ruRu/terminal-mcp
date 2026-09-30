import pytest

from terminal_mcp.fleet.config import FleetConfig
from terminal_mcp.fleet.projection import FleetProjectionService
from terminal_mcp.fleet.projection_storage import FleetProjectionStore
from terminal_mcp.fleet.protocol import canonical_event_id


class LocalSource:
    def __init__(self):
        self.high_water = 1
        self.state = "active"

    async def manifest(self):
        return {
            "fleet_id": "fleet-a",
            "node_id": "node-a",
            "source_stream_generation": "gen-a",
            "high_water_source_seq": self.high_water,
        }

    async def snapshot(self):
        return {
            "fleet_id": "fleet-a",
            "node_id": "node-a",
            "source_stream_generation": "gen-a",
            "barrier_source_seq": self.high_water,
            "complete_entity_types": ["logical_agent"],
            "entities": [
                {
                    "entity_type": "logical_agent",
                    "entity_id": "logical-1",
                    "entity_revision": self.high_water,
                    "payload_version": 1,
                    "payload": {"state": self.state},
                }
            ],
        }

    async def events(self, *, since, limit, source_stream_generation):
        del limit
        events = []
        if since < self.high_water:
            events.append(
                {
                    "event_id": canonical_event_id(
                        "fleet-a", "node-a", "gen-a", self.high_water
                    ),
                    "fleet_id": "fleet-a",
                    "node_id": "node-a",
                    "source_stream_generation": "gen-a",
                    "source_seq": self.high_water,
                    "event_type": "logical_agent.changed",
                    "entity_type": "logical_agent",
                    "entity_id": "logical-1",
                    "entity_revision": self.high_water,
                    "payload_version": 1,
                    "payload": {"state": self.state},
                    "created_at": "2026-01-01T00:00:00Z",
                }
            )
        return {
            "fleet_id": "fleet-a",
            "node_id": "node-a",
            "source_stream_generation": source_stream_generation,
            "events": events,
            "next_cursor": self.high_water,
            "reset_required": False,
        }

    async def runtime_health(self):
        return {
            "finalization_pending_commands": ["cmd-pending"],
            "stale_running_commands": ["cmd-stale"],
            "unowned_running_commands": ["cmd-stale"],
            "queues": [{"queue_id": 1, "durable_running": "cmd-stale"}],
        }


@pytest.mark.asyncio
async def test_owner_syncs_source_and_keeps_runtime_overlay_volatile(tmp_path):
    store = FleetProjectionStore(
        tmp_path / "projection.sqlite3",
        fleet_id="fleet-a",
        node_id="node-a",
        owner_node_id="node-a",
    )
    await store.initialize()
    source = LocalSource()
    service = FleetProjectionService(
        FleetConfig("node-a", "key", (), 1.0, 1.0),
        store,
        local_source=source,
        owner_node_id="node-a",
    )

    await service.sync_once()
    initial = await store.snapshot()
    initial_seq = initial["projection_seq"]
    logical = next(item for item in initial["entities"] if item["entity_type"] == "logical_agent")
    assert logical["payload"]["state"] == "active"
    assert initial["runtime_overlays"][0]["payload"]["unowned_running_commands"] == [
        "cmd-stale"
    ]

    await store.put_runtime_overlay(
        "node-a",
        {
            "finalization_pending_commands": [],
            "stale_running_commands": [],
            "unowned_running_commands": [],
            "queues": [],
        },
    )
    overlay_only = await store.snapshot()
    assert overlay_only["projection_seq"] == initial_seq

    source.high_water = 2
    source.state = "suspended"
    await service.sync_once()
    updated = await store.snapshot()
    assert updated["projection_seq"] == initial_seq + 1
    logical = next(item for item in updated["entities"] if item["entity_type"] == "logical_agent")
    assert logical["payload"]["state"] == "suspended"
