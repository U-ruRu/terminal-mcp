"""A replication gap obtains the exact current snapshot beyond the first page."""

import pytest
from test_access_mesh_native_lifecycle import actor
from test_access_mesh_native_lifecycle import fixture as fixture

from terminal_mcp.fleet.access_mesh import AccessMeshReplication
from terminal_mcp.fleet.config import FleetConfig, FleetPeer


@pytest.mark.asyncio
async def test_gap_outside_first_page_uses_exact_canonical_snapshot(fixture, monkeypatch):
    f = fixture
    issued = await f.app.issue(actor("access"), kind="persistent", code="4000")
    (expected,) = f.store.snapshot_page(issuer_id="firstbyte")
    peer = FleetPeer("bacloud", "https://bacloud", "fixture-key", "fixture-token")
    replication = AccessMeshReplication(
        f.app, FleetConfig("firstbyte", "fixture-key", (peer,), 1.0, 1.0)
    )
    monkeypatch.setattr(f.store, "snapshot_page", lambda **kwargs: [])
    restored = []

    async def request(peer, path, payload):
        if path == "events":
            return {"acked": [], "snapshot_slot_id": issued["slot_id"]}
        assert path == "snapshot/apply"
        restored.append(payload["slot"])
        return {"ok": True}

    monkeypatch.setattr(replication, "request", request)
    assert await replication._deliver(peer)
    assert restored == [expected]
