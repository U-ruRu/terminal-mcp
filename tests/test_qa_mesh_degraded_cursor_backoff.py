"""Regression: an interrupted paginated Mesh catch-up may not busy-loop retries."""

import asyncio
from types import SimpleNamespace

import pytest

from terminal_mcp.fleet.access_mesh import AccessMeshReplication


@pytest.mark.asyncio
async def test_degraded_peer_with_pending_snapshot_cursor_is_rate_limited():
    peer = SimpleNamespace(instance_id="unreachable-qa-peer")
    replication = object.__new__(AccessMeshReplication)
    replication.config = SimpleNamespace(peers=(peer,))
    replication.store = SimpleNamespace(trusted_issuers={peer.instance_id})
    replication._stop = asyncio.Event()
    replication._wake = asyncio.Event()
    replication._snapshot_after = {peer.instance_id: "page-1-cursor"}
    replication._last_snapshot_pass = {peer.instance_id: asyncio.get_running_loop().time()}
    replication.peer_health = {peer.instance_id: {"status": "degraded", "reason": "timeout"}}
    calls = 0

    async def failed_network_tick():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)

    replication.tick = failed_network_tick
    runner = asyncio.create_task(replication._loop())
    try:
        await asyncio.sleep(0.04)
    finally:
        replication._stop.set()
        replication._wake.set()
        await asyncio.wait_for(runner, timeout=1.0)
    assert calls <= 2, f"Degraded peer retried {calls} times in 40ms"
