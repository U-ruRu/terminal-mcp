"""Mesh admission hard deadline under imperfect network cancellation."""

import asyncio
from time import monotonic

import pytest
from test_access_mesh_replication_http import Routes, node

from terminal_mcp.fleet.access_mesh import AccessMeshReplication
from terminal_mcp.fleet.config import FleetConfig, FleetPeer


@pytest.mark.asyncio
async def test_remote_cancellation_does_not_extend_global_negotiation_deadline(
    tmp_path, monkeypatch
):
    """An unresponsive peer cannot extend the total issuer admission budget."""
    import httpx

    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        issuer, _ = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        peer = FleetPeer("bacloud", "https://bacloud", "key", "token")
        replica = AccessMeshReplication(issuer, FleetConfig("firstbyte", "key", (peer,), 1.0, 1.0))

        async def late_peer(*_args):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                # Real transports may finish a response while cancellation propagates.
                await asyncio.sleep(0.30)
                return {"ok": False, "code": "number_conflict", "suggested_number": "0098"}

        monkeypatch.setattr(replica, "request", late_peer)
        start = monotonic()
        number, attempt, _deadline = await replica.negotiate_number(
            preferred="0097", _budget_seconds=0.18
        )
        elapsed = monotonic() - start
        assert number == "0097"
        assert attempt
        assert elapsed < 0.29, f"30-second scaled hard cap violated: {elapsed:.3f}s"
