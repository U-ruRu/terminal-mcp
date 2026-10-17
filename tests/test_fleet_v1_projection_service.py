import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from terminal_mcp.fleet.config import FleetConfig
from terminal_mcp.fleet.projection import FleetProjectionService


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
async def test_query_relay_reports_partial_sources_when_a_peer_denies_access():
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

    assert by_source["node-auth"]["status"] == "OFFLINE_AUTH"


def test_command_query_redacts_credentials_and_shell_text():
    from terminal_mcp.fleet.projection import _redact_query_data

    result = _redact_query_data(
        {
            "items": [
                {
                    "hash": "cmd-a",
                    "status": "running",
                    "cmd": "private shell",
                    "auth_token": "secret",
                }
            ]
        },
        operation="query",
        resource="commands",
    )

    assert result == {"items": [{"hash": "cmd-a", "status": "running"}]}


@pytest.mark.asyncio
async def test_projection_worker_follows_peer_membership_and_stays_stopped_after_shutdown():
    from terminal_mcp.fleet.config import FleetPeer

    config = FleetConfig("local", "key", (), 60.0, 1.0)
    service = FleetProjectionService(config, SimpleNamespace(role="owner"), owner_node_id="local")
    synchronized = asyncio.Event()
    service.sync_once = AsyncMock(side_effect=synchronized.set)
    await service.start()
    async with service.namespace_transition():
        pass
    assert not service._namespace_suspended
    assert service._task is None
    service.sync_once.assert_not_awaited()
    joined = replace(config, peers=(FleetPeer("peer", "https://peer.test", "key", "token"),))
    service.apply_managed_fleet_config(joined)
    await asyncio.wait_for(synchronized.wait(), timeout=1)
    before_noop = service._task
    async with service.namespace_transition():
        pass
    assert service._task is not None and service._task is not before_noop
    previous = service._task
    service.apply_managed_fleet_config(config)
    assert service._task is None
    await asyncio.gather(previous, return_exceptions=True)
    assert previous.done()
    synchronized.clear()
    service.apply_managed_fleet_config(joined)
    await asyncio.wait_for(synchronized.wait(), timeout=1)
    await service.stop()
    service.apply_managed_fleet_config(config)
    service.apply_managed_fleet_config(joined)
    assert service._task is None and not service._retiring_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_role", ["owner", "follower"])
async def test_autonomous_projection_reads_fresh_local_data_without_http_client(
    tmp_path, previous_role
):
    from terminal_mcp.application.actor import ActorContext
    from terminal_mcp.application.replication import FleetProjectionApplication
    from terminal_mcp.fleet.projection_storage import FleetProjectionError, FleetProjectionStore

    class Source(RelayLocalSource):
        version = 1

        async def manifest(self):
            return {**await super().manifest(), "source_stream_generation": f"gen-{self.version}"}

        async def snapshot(self):
            return {
                **await self.manifest(),
                "barrier_source_seq": 0,
                "complete_entity_types": ["task"],
                "entities": [
                    {
                        "entity_type": "task",
                        "entity_id": "task",
                        "entity_revision": self.version,
                        "payload": {"title": f"version-{self.version}"},
                    }
                ],
            }

        async def runtime_health(self):
            return {"version": self.version}

        async def query(self, resource, **kwargs):
            return {"items": [{"title": f"version-{self.version}"}], "complete": True}

    def no_client():
        raise AssertionError("an autonomous local read must not create an HTTP client")

    previous_owner = "node-remote" if previous_role == "follower" else "node-local"
    store = FleetProjectionStore(
        tmp_path / "projection.db",
        fleet_id="fleet-a",
        node_id="node-local",
        owner_node_id=previous_owner,
        role=previous_role,
    )
    await store.initialize()
    await store.mark_source("node-remote", "gen-old", "fresh")
    await store.put_runtime_overlay("node-remote", {"old_mesh": True})
    source = Source()
    service = FleetProjectionService(
        FleetConfig("node-local", "key", (), 5.0, 1.0),
        store,
        local_source=source,
        owner_node_id="node-local",
        client_factory=no_client,
    )
    app = FleetProjectionApplication(store, service)
    actor = ActorContext(transport="mesh", endpoint_role="mesh", peer_node_id="peer")
    await service.start()
    first = await app.snapshot(actor)
    assert first["entities"][0]["payload"]["title"] == "version-1"
    assert first["owner_node_id"] == "node-local" and first["role"] == "owner"
    assert [row["source_node_id"] for row in first["sources"]] == ["node-local"]
    assert [row["source_node_id"] for row in first["runtime_overlays"]] == ["node-local"]
    source.version = 2
    second = await app.snapshot(actor)
    assert second["entities"][0]["payload"]["title"] == "version-2"
    assert second["projection_epoch"] == first["projection_epoch"]
    result = await service.query("tasks", limit=10)
    assert result["sources"][0]["data"]["items"] == [{"title": "version-2"}]
    assert service._task is None
    await service.stop()
    restarted = FleetProjectionStore(
        store.path,
        fleet_id="fleet-a",
        node_id="node-local",
        owner_node_id=previous_owner,
        role=previous_role,
    )
    await restarted.initialize()
    assert restarted.role == "owner" and restarted.owner_node_id == "node-local"
    assert not await restarted.adopt_local_cache()
    assert (await restarted.snapshot())["entities"] == second["entities"]
    restarted.fleet_id = "another-mesh"
    with pytest.raises(FleetProjectionError, match="identity does not match"):
        await restarted.initialize()


@pytest.mark.asyncio
async def test_follower_move_rebuilds_cache_from_the_current_mesh_sources(tmp_path):
    from terminal_mcp.fleet.config import FleetPeer
    from terminal_mcp.fleet.projection_storage import FleetProjectionStore

    class LocalSource(RelayLocalSource):
        async def snapshot(self):
            return {**await self.manifest(), "barrier_source_seq": 0, "entities": []}

        async def runtime_health(self):
            return {"current": True}

    store = FleetProjectionStore(
        tmp_path / "projection.db",
        fleet_id="fleet-a",
        node_id="node-local",
        owner_node_id="old-owner",
        role="follower",
    )
    await store.initialize()
    await store.mark_source("old-owner", "old-generation", "fresh")
    peer = FleetPeer("old-owner", "https://old.test", "key", "token")
    config = FleetConfig("node-local", "key", (peer,), 60.0, 1.0)
    service = FleetProjectionService(
        config,
        store,
        local_source=LocalSource(),
        owner_node_id="old-owner",
        client_factory=RelayClient,
    )
    new_peer = FleetPeer("new-peer", "https://new.test", "key", "token")
    visited = []

    async def current_remote_source(client, current_peer):
        visited.append(current_peer.instance_id)
        await store.mark_source(current_peer.instance_id, "new-generation", "fresh")

    service._sync_remote_source = current_remote_source
    service.apply_managed_fleet_config(replace(config, peers=(new_peer,)))
    await service.sync_once()

    assert visited == ["new-peer"]
    state = await store.snapshot()
    assert state["role"] == "owner" and state["owner_node_id"] == "node-local"
    assert {row["source_node_id"] for row in state["sources"]} == {"node-local", "new-peer"}


@pytest.mark.asyncio
async def test_namespace_transition_cancels_previous_source_write_before_reset():
    from terminal_mcp.fleet.config import FleetPeer

    config = FleetConfig(
        "local", "key", (FleetPeer("peer", "https://peer.test", "key", "token"),), 60.0, 1.0
    )
    service = FleetProjectionService(config, SimpleNamespace(role="owner"), owner_node_id="local")
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    writes = []

    async def old_namespace_write():
        entered.set()
        try:
            await asyncio.Event().wait()
            writes.append("old namespace")
        finally:
            cancelled.set()

    service._sync_owner = old_namespace_write
    await service.start()
    await asyncio.wait_for(entered.wait(), timeout=1)
    previous = service._task
    async with service.namespace_transition() as mark_applied:
        assert cancelled.is_set() and previous.done()
        writes.append("reset")
        mark_applied()
    await service.sync_once()
    assert writes == ["reset"] and service._task is None
    service.apply_managed_fleet_config(replace(config, peers=()))
    assert not service._namespace_suspended and service._task is None
    with pytest.raises(RuntimeError, match="interrupted metadata write"):
        async with service.namespace_transition():
            raise RuntimeError("interrupted metadata write")
    assert service._namespace_suspended
    await service.stop()
