"""Mesh admission hard deadline under imperfect network cancellation."""

import asyncio
from time import monotonic

import pytest
from test_access_mesh_replication_http import Routes, node

from terminal_mcp.core.access_mesh_grants import AccessMeshError
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

        attempts = []
        peer_tasks = []

        async def late_peer(_peer, _suffix, body):
            peer_tasks.append(asyncio.current_task())
            attempts.append(body["attempt_id"])
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                # Real transports may finish a response while cancellation propagates.
                await asyncio.sleep(0.30)
                return {"ok": False, "code": "number_conflict", "suggested_number": "0098"}

        monkeypatch.setattr(replica, "request", late_peer)
        start = monotonic()
        try:
            with pytest.raises(AccessMeshError, match="session_number_unconfirmed"):
                await replica.negotiate_number(preferred="0097", _budget_seconds=0.18)
            elapsed = monotonic() - start
            assert attempts and all(attempts)
            assert len(set(attempts)) == 1
            assert not issuer.store.numbers.reservations()
            assert not issuer.store.numbers.snapshot()
            assert not issuer.store.local_slots()
            assert elapsed < 0.29, f"30-second scaled hard cap violated: {elapsed:.3f}s"
        finally:
            # Drain cancellation-resistant fixture work after measuring and
            # asserting the public deadline, never as part of admission time.
            await asyncio.wait_for(
                asyncio.gather(*peer_tasks, return_exceptions=True), timeout=1.0
            )
