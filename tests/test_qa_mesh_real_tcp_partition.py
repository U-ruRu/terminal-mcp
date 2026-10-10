"""Independent SQLite peers over actual loopback HTTP, with a late joining node.

QA isolation: all databases are temporary, HTTP listeners bind only to loopback,
and no installed Firstbyte/Bacloud runtime or live access session is modified.
"""

from __future__ import annotations

import asyncio
import socket
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import uvicorn
from fastapi import FastAPI
from test_access_mesh_replication_http import KEY, Fence, actor

from terminal_mcp.application.access_mesh import AccessMeshApplication
from terminal_mcp.core.access_mesh_grants import SlotPolicy
from terminal_mcp.fleet.access_mesh import AccessMeshReplication, build_access_mesh_router
from terminal_mcp.fleet.config import FleetConfig, FleetPeer
from terminal_mcp.storage.access_mesh import AccessMeshStore
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore


def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _start_server(app: FastAPI, port: int, stack: AsyncExitStack) -> None:
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            lifespan="off",
            log_level="error",
            access_log=False,
        )
    )
    serve = asyncio.create_task(server.serve())

    async def stop():
        server.should_exit = True
        await asyncio.wait_for(serve, timeout=5)

    stack.push_async_callback(stop)
    for _ in range(300):
        if server.started:
            return
        if serve.done():
            await serve
            raise AssertionError("loopback server exited before accepting connections")
        await asyncio.sleep(0.01)
    raise AssertionError("loopback server startup timed out")


@pytest.mark.asyncio
async def test_partition_collision_converges_with_real_tcp_and_third_node(tmp_path):
    names = ("qa-firstbyte", "qa-bacloud", "qa-newnode")
    ports = {name: _unused_port() for name in names}
    now = [datetime.now(UTC).replace(microsecond=0)]
    nodes = {}
    for name in names:
        path = tmp_path / f"{name}.sqlite3"
        await SqliteRepository(path, tmp_path / f"{name}-output.sqlite3").initialize()
        store = AccessMeshStore(
            path,
            local_node_id=name,
            trusted_issuers=frozenset(names),
            proof_key=KEY,
        )
        mesh = AccessMeshApplication(
            store,
            task_store=TaskStore(path),
            execution_fence=Fence(),
            clock=lambda: now[0],
        )
        peers = tuple(
            FleetPeer(
                other,
                f"http://127.0.0.1:{ports[other]}",
                "fixture-key",
                "fixture-token",
            )
            for other in names
            if other != name
        )
        config = FleetConfig(name, "fixture-key", peers, 1.0, 1.0)
        known = {peer.instance_id: peer for peer in peers}

        def auth(peer_id, authorization, *, allowed=known):
            p = allowed.get(peer_id)
            return p if p and authorization == "Bearer fixture-token" else None

        app = FastAPI()
        app.include_router(build_access_mesh_router(mesh, SimpleNamespace(authenticate=auth)))
        nodes[name] = (mesh, AccessMeshReplication(mesh, config), app)

    first, first_rep, first_app = nodes["qa-firstbyte"]
    second, second_rep, second_app = nodes["qa-bacloud"]
    third, third_rep, third_app = nodes["qa-newnode"]
    # With all TCP endpoints disconnected, both independent issuers allocate
    # the same number and keep separate original identities.
    original = await first.issue(actor(), code="0356", policy=SlotPolicy(300))
    now[0] += timedelta(seconds=15)
    later = await second.issue(actor(), code="0356", policy=SlotPolicy(600))
    assert original["logical_agent_id"] != later["logical_agent_id"]
    incident = first.store.numbers.record_late_conflict(
        number="0356",
        attempt_id="nr_tcp-test-partition",
        peer_id="qa-bacloud",
        suggested_number="0998",
    )
    assert incident["ok"]
    reserved = first.store.numbers.reserve(
        number="0357", attempt_id="nr_tcp-test-reservation", now=now[0]
    )
    assert reserved["ok"]

    async with AsyncExitStack() as stack:
        # Join two real HTTP listeners first; third still network-unreachable.
        await _start_server(first_app, ports["qa-firstbyte"], stack)
        await _start_server(second_app, ports["qa-bacloud"], stack)
        for _ in range(2):
            await asyncio.gather(first_rep.tick(), second_rep.tick())
        assert first.store.numbers.winner("0356")["logical_agent_id"] == later["logical_agent_id"]
        assert second.store.numbers.winner("0356")["logical_agent_id"] == later["logical_agent_id"]
        assert not third.store.numbers.snapshot()

        # A fresh node receives claims, reservations and incident metadata.
        await _start_server(third_app, ports["qa-newnode"], stack)
        for _ in range(2):
            await asyncio.gather(first_rep.tick(), second_rep.tick(), third_rep.tick())
        for mesh, _, _ in nodes.values():
            winner = mesh.store.numbers.winner("0356")
            assert winner["logical_agent_id"] == later["logical_agent_id"]
            assert winner["hard_expires_at"] == mesh.store.numbers.stamp(
                now[0] + timedelta(seconds=600)
            )
        assert len(third.store.numbers.snapshot()) == 2
        assert third.store.numbers.incidents()
        assert third.store.numbers.reservations()
        assert (
            first.store.slot("qa-firstbyte", original["slot_id"]).logical_agent_id
            == original["logical_agent_id"]
        )
        assert (
            second.store.slot("qa-bacloud", later["slot_id"]).logical_agent_id
            == later["logical_agent_id"]
        )
        assert await third.attach(actor(), session_number="0356") == {"ok": True}
        attached = third.store.attached_slot(third.connection_key(actor()))
        assert attached.logical_agent_id == later["logical_agent_id"]
        await third_rep.tick()
        assert len(third.store.numbers.snapshot()) == 2
