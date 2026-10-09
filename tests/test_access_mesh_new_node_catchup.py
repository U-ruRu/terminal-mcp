"""Acceptance: a newly joining node converges after a multi-issuer partition."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from test_access_mesh_replication_http import (
    KEY,
    Fence,
    Routes,
    actor,
)

from terminal_mcp.application.access_mesh import AccessMeshApplication
from terminal_mcp.core.access_mesh_grants import SlotPolicy
from terminal_mcp.fleet.access_mesh import (
    AccessMeshReplication,
    build_access_mesh_router,
)
from terminal_mcp.fleet.config import FleetConfig, FleetPeer
from terminal_mcp.storage.access_mesh import AccessMeshStore
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore


async def three_node_cluster(tmp_path, client, routes, *, now):
    names = ("firstbyte", "bacloud", "newnode")
    nodes = {}
    for name in names:
        path = tmp_path / f"mesh-{name}.sqlite3"
        await SqliteRepository(path, tmp_path / f"output-{name}.sqlite3").initialize()
        store = AccessMeshStore(
            path,
            local_node_id=name,
            trusted_issuers=frozenset(names),
            proof_key=KEY,
        )
        runtime = AccessMeshApplication(
            store,
            task_store=TaskStore(path),
            execution_fence=Fence(),
            clock=lambda: now[0],
        )
        peers = tuple(
            FleetPeer(other, f"https://{other}", "fixture-key", "fixture-token")
            for other in names
            if other != name
        )
        config = FleetConfig(name, "fixture-key", peers, 1.0, 1.0)
        allow = {peer.instance_id: peer for peer in peers}

        def authenticated(peer_id, auth, *, allowed=allow):
            match = allowed.get(peer_id)
            return match if match and auth == "Bearer fixture-token" else None

        app = FastAPI()
        app.include_router(
            build_access_mesh_router(runtime, SimpleNamespace(authenticate=authenticated))
        )
        routes.apps[name] = app
        replica = AccessMeshReplication(runtime, config, client=client)
        runtime.replication = replica
        nodes[name] = (runtime, replica)
    return nodes


@pytest.mark.asyncio
async def test_new_node_catches_collisions_reservations_and_slots_without_rewriting_history(
    tmp_path,
):
    routes = Routes()
    clock = [datetime.now(UTC).replace(microsecond=0)]
    async with httpx.AsyncClient(transport=routes) as client:
        nodes = await three_node_cluster(tmp_path, client, routes, now=clock)
        first, f_rep = nodes["firstbyte"]
        second, s_rep = nodes["bacloud"]
        newcomer, n_rep = nodes["newnode"]
        # New node absent when the same four digits are issued independently.
        routes.offline.add("newnode")
        original = await first.issue(actor(), code="0356", policy=SlotPolicy(300))
        clock[0] += timedelta(seconds=15)
        later = await second.issue(actor(), code="0356", policy=SlotPolicy(600))
        assert original["logical_agent_id"] != later["logical_agent_id"]
        number = "0356"
        # A delayed conflict is an audit record, not a grant. A joining node
        # must recover it even if its original issuer is offline at creation.
        late = first.store.numbers.record_late_conflict(
            number="0356",
            attempt_id="nr_delayed-partition",
            peer_id="bacloud",
            suggested_number="0998",
        )
        assert late["ok"]
        expected_late = first.store.numbers.incidents()[0]

        # Independently reserved temporary numbers also must be caught up.
        reserve = first.store.numbers.reserve(
            number="0357", attempt_id="nr_pending-mesh-test", now=clock[0]
        )
        assert reserve["ok"], reserve
        # The two existing issuers can reconcile without the third node.
        await f_rep.tick()
        await s_rep.tick()
        assert first.store.numbers.winner(number)["logical_agent_id"] == later["logical_agent_id"]
        assert not newcomer.store.numbers.snapshot()

        routes.offline.remove("newnode")
        await n_rep.tick()
        await f_rep.tick()
        await s_rep.tick()
        await n_rep.tick()
        all_current = [mesh.store.numbers.winner(number) for mesh, _ in nodes.values()]
        assert all(item["logical_agent_id"] == later["logical_agent_id"] for item in all_current), (
            all_current
        )
        assert all(
            item["hard_expires_at"] == first.store.numbers.stamp(clock[0] + timedelta(seconds=600))
            for item in all_current
        )
        assert len(newcomer.store.numbers.snapshot()) == 2
        assert newcomer.store.numbers.incidents()
        assert expected_late in newcomer.store.numbers.incidents()
        assert newcomer.store.numbers.reservations()

        # The independent issuer histories remain unchanged, even after
        # retroactively electing a shared current LogicalAgent.
        assert (
            first.store.slot("firstbyte", original["slot_id"]).logical_agent_id
            == (original["logical_agent_id"])
        )
        assert (
            second.store.slot("bacloud", later["slot_id"]).logical_agent_id
            == (later["logical_agent_id"])
        )
        assert (await newcomer.attach(actor(), session_number=number)) == {"ok": True}
        bound = newcomer.store.attached_slot(newcomer.connection_key(actor()))
        assert bound.logical_agent_id == later["logical_agent_id"]
        # An idempotent replay must not create duplicate slots or claims.
        await n_rep.tick()
        assert len(newcomer.store.numbers.snapshot()) == 2
        assert (
            newcomer.store.slot("firstbyte", original["slot_id"]).slot_id == (original["slot_id"])
        )

        restarted = AccessMeshStore(
            newcomer.store.path,
            local_node_id="newnode",
            trusted_issuers=frozenset(nodes),
            proof_key=KEY,
        )
        assert restarted.numbers.snapshot() == newcomer.store.numbers.snapshot()
        assert restarted.numbers.incidents() == newcomer.store.numbers.incidents()
        assert restarted.numbers.reservations() == newcomer.store.numbers.reservations()
