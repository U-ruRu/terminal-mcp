"""A losing attachment remains reachable under its current collision winner."""

from datetime import timedelta

import pytest
from test_access_mesh_native_lifecycle import T0, actor
from test_access_mesh_native_lifecycle import fixture as fixture

from terminal_mcp.application.access_mesh_messages import AccessMeshMessaging
from terminal_mcp.core.access_mesh_grants import AccessSlotEvent, SlotPolicy
from terminal_mcp.core.human_names import number_agent_name
from terminal_mcp.storage.agents import AgentStore


@pytest.mark.asyncio
async def test_losing_attachment_is_listed_as_current_winner(fixture):
    f = fixture
    await f.app.issue(actor("access"), kind="persistent", code="0672")
    await f.app.attach(actor(), issuer_node_id="firstbyte", access_code="0672")
    started = T0 + timedelta(seconds=1)
    event = AccessSlotEvent(
        issuer_id="bacloud",
        slot_id="winning-slot",
        logical_agent_id="winning-agent",
        revision=1,
        event_id="winning-issued",
        kind="SlotIssued",
        policy=SlotPolicy(1380),
        code_tag=f.store.code_tag("bacloud", "0672"),
        effective_at=started,
        agent_name=number_agent_name("0672"),
    )
    f.store.apply_event(
        event,
        authenticated_peer_id="bacloud",
        issued_kind="persistent",
        mutation_number={"number": "0672", "started_at": started, "attempt_id": "winning-attempt"},
    )
    f.clock[0] = started
    winner = f.store.slot("bacloud", "winning-slot")
    f.store.local_identity(winner, now=started)
    messaging = AccessMeshMessaging(f.app, AgentStore(f.store.path))
    recipients = await messaging._local_recipients()
    assert [recipient["logical_agent_id"] for recipient in recipients] == ["winning-agent"]
