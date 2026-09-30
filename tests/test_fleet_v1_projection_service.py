import pytest

from terminal_mcp.fleet.config import FleetConfig
from terminal_mcp.fleet.projection import FleetProjectionService
from terminal_mcp.fleet.projection_storage import FleetProjectionStore
from terminal_mcp.fleet.protocol import canonical_event_id


class LocalSource:
    def __init__(self):
        self.high_water = 1
        self.state = "active"
        self.fail_health = False

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
        if self.fail_health:
            raise RuntimeError("runtime unavailable")
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

    durable_seq = updated["projection_seq"]
    source.fail_health = True
    await service.sync_once()
    degraded = await store.snapshot()
    assert degraded["projection_seq"] == durable_seq
    assert degraded["sources"][0]["freshness"] == "unavailable"
    assert degraded["runtime_overlays"][0]["freshness"] == "stale"
    logical = next(
        item for item in degraded["entities"] if item["entity_type"] == "logical_agent"
    )
    assert logical["payload"]["state"] == "suspended"


class ManyResponse:
    def __init__(self, body):
        self.body = body
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class ManySourceClient:
    def __init__(self):
        self.requests = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url, *, headers, params=None):
        del headers, params
        self.requests += 1
        node = url.split("//", 1)[1].split(".", 1)[0]
        if url.endswith("/manifest"):
            return ManyResponse(
                {
                    "fleet_id": "fleet-a",
                    "node_id": node,
                    "source_stream_generation": "gen-a",
                    "high_water_source_seq": 0,
                }
            )
        if url.endswith("/snapshot"):
            return ManyResponse(
                {
                    "fleet_id": "fleet-a",
                    "node_id": node,
                    "source_stream_generation": "gen-a",
                    "barrier_source_seq": 0,
                    "complete_entity_types": [],
                    "entities": [],
                }
            )
        if url.endswith("/runtime-health"):
            return ManyResponse(
                {
                    "finalization_pending_commands": [],
                    "stale_running_commands": [],
                    "unowned_running_commands": [],
                    "queues": [],
                }
            )
        raise AssertionError(url)


@pytest.mark.asyncio
@pytest.mark.parametrize("source_count", [50, 200])
async def test_owner_source_fanout_is_linear_in_sources(tmp_path, source_count):
    from terminal_mcp.fleet.config import FleetPeer

    peers = tuple(
        FleetPeer(
            f"node-{index}",
            f"https://node-{index}.example",
            "public",
            "token",
        )
        for index in range(source_count)
    )
    client = ManySourceClient()
    store = FleetProjectionStore(
        tmp_path / "projection.sqlite3",
        fleet_id="fleet-a",
        node_id="projection-a",
        owner_node_id="projection-a",
    )
    await store.initialize()
    service = FleetProjectionService(
        FleetConfig("projection-a", "key", peers, 1.0, 1.0),
        store,
        owner_node_id="projection-a",
        client_factory=lambda: client,
    )
    await service.sync_once()
    assert client.requests == source_count * 3
    state = await store.snapshot()
    assert len(state["sources"]) == source_count


class V2LocalSource(LocalSource):
    def __init__(self):
        super().__init__()
        self.snapshot_calls = 0
        self.events_calls = 0
        self.present = True

    async def manifest(self):
        result = await super().manifest()
        result["capabilities"] = [
            "fleet.source.current-recovery.v2",
            "fleet.source.current-entity.v2",
        ]
        return result

    async def snapshot(self):
        self.snapshot_calls += 1
        raise AssertionError("v2 owner must not use global source snapshot")

    async def bootstrap(self):
        return {
            "fleet_id": "fleet-a",
            "node_id": "node-a",
            "source_stream_generation": "gen-a",
            "scope_version": 1,
            "current_scopes": ["logical_agents"],
            "barrier_source_seq": self.high_water,
            "high_water_source_seq": self.high_water,
        }

    def _entity(self):
        if not self.present:
            return None
        return {
            "entity_type": "logical_agent",
            "entity_id": "logical-1",
            "entity_revision": self.high_water,
            "payload_version": 2,
            "payload": {"state": self.state, "display_name": "Complete Agent"},
        }

    async def current_recovery(
        self,
        scope,
        *,
        source_stream_generation,
        snapshot_id=None,
        cursor=None,
        limit=100,
        barrier_source_seq=None,
    ):
        del cursor, limit
        assert scope == "logical_agents"
        assert source_stream_generation == "gen-a"
        barrier = int(barrier_source_seq)
        return {
            "fleet_id": "fleet-a",
            "node_id": "node-a",
            "source_stream_generation": "gen-a",
            "scope": scope,
            "scope_version": 1,
            "snapshot_id": snapshot_id or f"snap-{barrier}",
            "barrier_source_seq": barrier,
            "high_water_source_seq": self.high_water,
            "page_complete": True,
            "page_rows": 1 if self.present else 0,
            "page_bytes": 64,
            "entities": [self._entity()] if self.present else [],
            "reset_required": False,
        }

    async def current_entity(self, scope, entity_id, *, source_stream_generation):
        assert scope == "logical_agents"
        assert entity_id == "logical-1"
        assert source_stream_generation == "gen-a"
        return {
            "fleet_id": "fleet-a",
            "node_id": "node-a",
            "source_stream_generation": "gen-a",
            "scope": scope,
            "entity_id": entity_id,
            "entity": self._entity(),
            "reset_required": False,
        }

    async def events(self, *, since, limit, source_stream_generation):
        self.events_calls += 1
        assert source_stream_generation == "gen-a"
        end = min(self.high_water, since + limit)
        events = [
            {
                "event_id": canonical_event_id("fleet-a", "node-a", "gen-a", seq),
                "fleet_id": "fleet-a",
                "node_id": "node-a",
                "source_stream_generation": "gen-a",
                "source_seq": seq,
                "event_type": "logical_agent.changed",
                "entity_type": "logical_agent",
                "entity_id": "logical-1",
                "entity_revision": seq,
                "payload_version": 1,
                "payload": {"state": "partial-only"},
                "created_at": "2026-01-01T00:00:00Z",
            }
            for seq in range(since + 1, end + 1)
        ]
        return {
            "fleet_id": "fleet-a",
            "node_id": "node-a",
            "source_stream_generation": "gen-a",
            "events": events,
            "next_cursor": end,
            "high_water_source_seq": self.high_water,
            "reset_required": False,
        }


@pytest.mark.asyncio
async def test_v2_owner_recovers_scoped_then_catches_up_multi_page_without_global_snapshot(
    tmp_path,
):
    store = FleetProjectionStore(
        tmp_path / "projection-v2.sqlite3",
        fleet_id="fleet-a",
        node_id="node-a",
        owner_node_id="node-a",
    )
    await store.initialize()
    source = V2LocalSource()
    service = FleetProjectionService(
        FleetConfig("node-a", "key", (), 1.0, 1.0),
        store,
        local_source=source,
        owner_node_id="node-a",
    )

    await service.sync_once()
    initial = await store.snapshot()
    assert source.snapshot_calls == 0
    assert initial["sources"][0]["source_seq"] == 1
    assert initial["scope_statuses"][0]["status"] == "LIVE"

    source.high_water = 301
    source.state = "suspended"
    await service.sync_once()
    caught_up = await store.snapshot()
    assert source.snapshot_calls == 0
    assert source.events_calls >= 3
    assert caught_up["sources"][0]["source_seq"] == 301
    logical = next(item for item in caught_up["entities"] if item["entity_id"] == "logical-1")
    assert logical["payload"] == {"state": "suspended", "display_name": "Complete Agent"}

    source.high_water = 302
    source.present = False
    await service.sync_once()
    removed = await store.snapshot()
    assert not [item for item in removed["entities"] if item["entity_id"] == "logical-1"]


class RelayLocalSource:
    async def manifest(self):
        return {
            "fleet_id": "fleet-a",
            "node_id": "node-local",
            "source_stream_generation": "gen-a",
        }

    async def query(self, resource, **kwargs):
        assert resource == "commands"
        assert kwargs["limit"] == 10
        return {
            "resource": resource,
            "items": [
                {
                    "hash": "cmd-local",
                    "cmd": "printf secret",
                    "status": "completed",
                    "auth_principal_id": "principal-secret",
                }
            ],
            "complete": True,
        }


class RelayResponse:
    def __init__(self, body, status_code=200):
        self.body = body
        self.status_code = status_code
        self.request = None

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            request = httpx.Request("GET", "https://relay.example/query")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(
                "relay failed",
                request=request,
                response=response,
            )

    def json(self):
        return self.body


class RelayClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url, *, headers, params=None):
        del headers, params
        if "node-good.example" in url:
            return RelayResponse(
                {
                    "resource": "commands",
                    "items": [
                        {
                            "hash": "cmd-good",
                            "cmd": "cat private",
                            "status": "running",
                            "auth_token": "must-not-escape",
                        }
                    ],
                    "complete": True,
                }
            )
        if "node-auth.example" in url:
            return RelayResponse({}, 401)
        raise AssertionError(url)


@pytest.mark.asyncio
async def test_query_relay_preserves_source_provenance_partial_status_and_redaction():
    from terminal_mcp.fleet.config import FleetPeer

    peers = (
        FleetPeer("node-good", "https://node-good.example", "public", "token"),
        FleetPeer("node-auth", "https://node-auth.example", "public", "token"),
    )
    service = FleetProjectionService(
        FleetConfig("projection-a", "key", peers, 1.0, 1.0),
        object(),
        local_source=RelayLocalSource(),
        owner_node_id="projection-a",
        client_factory=RelayClient,
    )

    result = await service.query("commands", limit=10)
    assert result["partial"] is True
    assert result["complete"] is False
    by_source = {item["source_node_id"]: item for item in result["sources"]}
    assert set(by_source) == {"node-local", "node-good", "node-auth"}

    local_item = by_source["node-local"]["data"]["items"][0]
    assert local_item["hash"] == "cmd-local"
    assert "cmd" not in local_item
    assert "auth_principal_id" not in local_item

    remote_item = by_source["node-good"]["data"]["items"][0]
    assert remote_item["hash"] == "cmd-good"
    assert "cmd" not in remote_item
    assert "auth_token" not in remote_item

    assert by_source["node-auth"]["ok"] is False
    assert by_source["node-auth"]["status"] == "OFFLINE_AUTH"
