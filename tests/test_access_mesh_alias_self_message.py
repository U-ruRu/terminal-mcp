"""A converged Mesh LogicalAgent cannot message one of its historic aliases."""

import httpx
import pytest
from test_access_mesh_replication_http import Routes, actor, node

from terminal_mcp.application.access_mesh_messages import AccessMeshMessaging, _public_name
from terminal_mcp.core.managed_sessions import ManagedOperation
from terminal_mcp.storage.agents import AgentStore


@pytest.mark.asyncio
async def test_merged_alias_is_self_and_not_a_second_recipient(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        first, f_rep = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        second, s_rep = await node(tmp_path, "bacloud", "firstbyte", routes, client)
        losing_grant = await second.issue(actor(), code="0888")
        await second.attach(actor(), issuer_node_id="bacloud", access_code="0888")
        original_slot = second.store.slot("bacloud", losing_grant["slot_id"])
        old_name = _public_name(original_slot)
        winning_grant = await first.issue(actor(), code="0888")
        await f_rep.tick()
        await s_rep.tick()
        canonical = second.store.attached_slot(second.connection_key(actor()))
        assert canonical.logical_agent_id == winning_grant["logical_agent_id"]
        assert canonical.logical_agent_id != losing_grant["logical_agent_id"]
        await second.resolve(actor(), ManagedOperation.COMMAND_RUN)
        messages = AccessMeshMessaging(second, AgentStore(second.store.path))
        recipients = await messages._local_recipients()
        assert all(
            row["logical_agent_id"] != losing_grant["logical_agent_id"] for row in recipients
        )
        rejected = await messages.message(
            actor(),
            scope="local",
            text="Do not duplicate",
            target=old_name,
        )
        assert rejected["ok"] is False
        assert rejected["code"] == "cannot_message_self"
        with messages.store.connect() as db:
            assert db.execute("SELECT count(*) FROM coordination_messages").fetchone()[0] == 0
        assert (
            second.store.slot("bacloud", losing_grant["slot_id"]).logical_agent_id
            == losing_grant["logical_agent_id"]
        )


@pytest.mark.asyncio
async def test_remote_only_losing_alias_cannot_be_messaged_by_shared_agent(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        first, f_rep = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        second, s_rep = await node(tmp_path, "bacloud", "firstbyte", routes, client)
        a = await first.issue(actor(), code="0889")
        b = await second.issue(actor(), code="0889")
        await f_rep.tick()
        await s_rep.tick()
        assert first.store.numbers.winner("0889")["logical_agent_id"] == a["logical_agent_id"]
        assert a["logical_agent_id"] != b["logical_agent_id"]
        # Only the winner is attached locally; the loser's alias is remote-only.
        await first.attach(actor(), session_number="0889")
        losing = second.store.slot("bacloud", b["slot_id"])
        old_name = _public_name(losing)
        messages = AccessMeshMessaging(first, AgentStore(first.store.path))
        before = await messages._local_recipients()
        assert len(before) == 1
        response = await messages.message(
            actor(), scope="local", text="must not send", target=old_name
        )
        assert response["code"] == "cannot_message_self"
        with messages.store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM coordination_messages").fetchone()[0] == 0
