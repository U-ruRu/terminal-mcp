"""Gap recovery beyond the first snapshot page uses the canonical wire projection."""

import json

import httpx
import pytest
from test_access_mesh_replication_http import T0, Routes, actor, node


@pytest.mark.asyncio
async def test_gap_after_first_fifty_slots_uses_canonical_snapshot(tmp_path):
    class RecordingRoutes(Routes):
        def __init__(self):
            super().__init__()
            self.applied_snapshots = []

        async def handle_async_request(self, request):
            if request.url.path.endswith("/access-mesh/snapshot/apply"):
                self.applied_snapshots.append(json.loads(request.content)["slot"])
            return await super().handle_async_request(request)

    routes = RecordingRoutes()
    async with httpx.AsyncClient(transport=routes) as client:
        source, replication = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        destination, _ = await node(tmp_path, "bacloud", "firstbyte", routes, client)
        source.store.clock = destination.store.clock = lambda: T0
        peer = replication.peers[0]
        slots = []
        for number in range(4000, 4051):
            issued = await source.issue(actor(), code=str(number), inherit_policy=False)
            slots.append(issued["slot_id"])
        for _ in range(3):
            assert await replication._deliver(peer) is True
        assert not source.store.pending_outbox(peer_node_id="bacloud")

        # Slot IDs are random; select by the same ordering as snapshot_page.
        slot_id = max(slots)
        first_page = source.store.snapshot_page(issuer_id="firstbyte", limit=50)
        assert len(first_page) == 50
        assert slot_id not in {row["slot_id"] for row in first_page}
        assert destination.store.slot("firstbyte", slot_id).revision == 1
        await source.change(actor(), slot_id=slot_id, kind="SlotSuspended", expected_revision=1)
        missing = source.store.pending_outbox(peer_node_id="bacloud")[0]
        # Simulate an ACKed revision that was lost when the receiver was restored.
        source.store.acknowledge_delivery(
            peer_node_id="bacloud",
            event_id=missing.event.event_id,
            authenticated_peer_id="bacloud",
        )
        await source.change(actor(), slot_id=slot_id, kind="SlotResumed", expected_revision=2)
        expected = source.store.snapshot_page(
            issuer_id="firstbyte", after=first_page[-1]["slot_id"], limit=1
        )[0]
        assert expected["slot_id"] == slot_id

        # Exercise events -> gap response -> exact-row fallback -> snapshot/apply.
        # Avoid periodic anti-entropy, which could repair the gap before this path.
        assert await replication._deliver(peer) is True
        assert routes.applied_snapshots == [expected]
        assert "code_generation" not in routes.applied_snapshots[0]
        assert "inherit_policy" not in routes.applied_snapshots[0]
        assert "policy_window_json" not in routes.applied_snapshots[0]
        restored = destination.store.slot("firstbyte", slot_id)
        assert restored.revision == 3
        assert restored.state == "active"
        assert restored.logical_agent_id == source.store.slot("firstbyte", slot_id).logical_agent_id

        # The next pass receives a duplicate ACK and removes the durable outbox item.
        assert await replication._deliver(peer) is True
        assert not source.store.pending_outbox(peer_node_id="bacloud")
        assert len(routes.applied_snapshots) == 1
