"""A replicated selector rotation accepts a new connection without replacing its slot."""

import pytest
from test_access_mesh_native_lifecycle import actor
from test_access_mesh_native_lifecycle import fixture as fixture

from terminal_mcp.core.access_mesh_grants import AccessSlotEvent


@pytest.mark.asyncio
async def test_rotated_replica_code_attaches_to_existing_slot(fixture):
    f = fixture
    issued = await f.app.issue(actor("access"), kind="persistent", code="1234")
    original = f.store.slot("firstbyte", issued["slot_id"])
    f.store.apply_event(
        AccessSlotEvent(
            issuer_id="firstbyte",
            slot_id=original.slot_id,
            logical_agent_id=original.session.logical_agent_id,
            revision=2,
            event_id="rotate-event",
            kind="AccessCodeRotated",
            code_tag=f.store.code_tag("firstbyte", "5678"),
        ),
        authenticated_peer_id="firstbyte",
    )
    attached = f.store.attach(issuer_id="firstbyte", code="5678", connection_key="new-connection")
    assert attached.session.logical_agent_id == original.session.logical_agent_id
    assert attached.slot_id == original.slot_id
