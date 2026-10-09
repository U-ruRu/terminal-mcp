"""A pre-existing losing attach remains a reachable recipient after number merger."""

from dataclasses import replace

import httpx
import pytest
from test_access_mesh_replication_http import Routes, actor, node

from terminal_mcp.application.access_mesh_messages import AccessMeshMessaging
from terminal_mcp.core.managed_sessions import ManagedOperation
from terminal_mcp.storage.agents import AgentStore


@pytest.mark.asyncio
async def test_preexisting_losing_attach_is_visible_under_winner_without_reconnect(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        first, f_rep = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        second, s_rep = await node(tmp_path, "bacloud", "firstbyte", routes, client)
        loser = await second.issue(actor(), code="0672")
        assert (await second.attach(actor(), session_number="0672")) == {"ok": True}
        before = await second.resolve(actor(), ManagedOperation.COMMAND_RUN)
        assert before["logical_agent_id"] == loser["logical_agent_id"]
        winner = await first.issue(actor(), code="0672")
        assert winner["logical_agent_id"] != loser["logical_agent_id"]
        await f_rep.tick()
        await s_rep.tick()
        assert (
            second.store.attached_slot(second.connection_key(actor())).logical_agent_id
            == winner["logical_agent_id"]
        )
        messaging = AccessMeshMessaging(second, AgentStore(second.store.path))
        listed = await messaging._local_recipients()
        assert len(listed) == 1 and listed[0]["logical_agent_id"] == winner["logical_agent_id"], (
            listed
        )
        assert listed[0]["public_name"] == winner["public_name"]
        second.messages = messaging
        observed = await second.observe(actor())
        assert observed["ok"], observed
        assert len(observed["agents"]) == 1
        agent = observed["agents"][0]
        assert set(agent) == {"public_name", "last_server", "last_activity", "session_duration"}
        assert agent["public_name"] == winner["public_name"]
        assert agent["last_server"] == "bacloud"

        # A different local agent must be able to address the *winner* while
        # the winning identity is still bound only through a losing alias.
        another = replace(
            actor(),
            principal_id="another-fixture-user",
            provider_metadata={
                "openai/subject": "another-fixture-user",
                "openai/session": "another-conversation",
            },
        )
        separate = await second.issue(another, code="0673")
        assert separate["logical_agent_id"] != winner["logical_agent_id"]
        assert (await second.attach(another, session_number="0673")) == {"ok": True}
        sent = await messaging.message(
            another,
            text="New sender to the converged agent",
            target=winner["public_name"],
            scope="local",
            mode="notify",
        )
        assert sent["ok"], sent
        recipient = messaging.store.recipient(sent["message_hash"], winner["logical_agent_id"])
        assert recipient is not None


@pytest.mark.asyncio
async def test_multiple_old_and_winning_attachments_keep_latest_activity_on_one_node(tmp_path):
    from datetime import timedelta

    from test_access_mesh_replication_http import T0

    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        first, f_rep = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        second, s_rep = await node(tmp_path, "bacloud", "firstbyte", routes, client)
        losing_slot = await second.issue(actor(), code="0792")
        assert (await second.attach(actor(), session_number="0792")) == {"ok": True}
        winning_slot = await first.issue(actor(), code="0792")
        await f_rep.tick()
        await s_rep.tick()
        second_conn = replace(
            actor(),
            principal_id="other-fixture",
            provider_metadata={"openai/subject": "other", "openai/session": "secondary"},
        )
        assert (await second.attach(second_conn, session_number="0792")) == {"ok": True}
        assert (
            second.store.attached_slot(second.connection_key(second_conn)).logical_agent_id
            == winning_slot["logical_agent_id"]
        )
        loser_connection = second.connection_key(actor())
        second.store.touch(loser_connection, now=T0 + timedelta(seconds=4))
        second.store.touch(second.connection_key(second_conn), now=T0 + timedelta(seconds=2))
        messaging = AccessMeshMessaging(second, AgentStore(second.store.path))
        recipients = await messaging._local_recipients()
        assert (
            len(recipients) == 1
            and recipients[0]["logical_agent_id"] == (winning_slot["logical_agent_id"])
        )
        assert recipients[0]["last_active_at"] == (T0 + timedelta(seconds=4)).isoformat(
            timespec="microseconds"
        )
        assert losing_slot["logical_agent_id"] != winning_slot["logical_agent_id"]
