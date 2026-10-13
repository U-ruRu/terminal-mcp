"""Events use the live Access Mesh trust, including membership changes after startup."""

import base64
import hashlib
import hmac
import json
import sqlite3

import httpx
import jsonschema
import pytest
from test_mcp_events_fleet_lifecycle import (
    KEY,
    SECRET,
    Routes,
    allowed,
    local_message,
    message_subscription,
    node,
    records,
    subscribe,
)

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.mcp.event_server import MCPEventsAdapter
from terminal_mcp.mcp.event_store import event_catalog


async def membership(n, names, *, authority=None):
    # The same verified Fleet Control entry point used by managed enrollment.
    await n.mesh.apply_fleet_policy(
        {
            "managed": True,
            "control_node_id": authority or n.config.instance_id,
            "nodes": [
                {"node_id": name, "state": "active", "mesh_id": "fixture-mesh"} for name in names
            ],
            "policy": {
                "duration_seconds": 1200,
                "rearm_after_seconds": 60,
                "warning_after_seconds": 1080,
                "alert_after_seconds": 1170,
                "revision": 1,
                "legacy_admission_enabled": True,
            },
        }
    )
    if n.mesh.managed_trust_enabled:
        n.mesh.store.install_managed_proof(
            mesh_id="fixture-mesh",
            authority_id=authority or n.config.instance_id,
            ingress_token=KEY.decode(),
        )
    n.replication.apply_managed_fleet_config(n.config)


async def issue_recipient(access):
    actor = ActorContext(
        auth_mode="bearer",
        principal_id="fixture-user",
        credential_id="fixture",
        auth_generation=1,
        scopes=frozenset({"terminal:read", "terminal:execute"}),
        provider="openai",
        provider_metadata={"openai/subject": "s", "openai/session": "c"},
        transport="mcp",
        endpoint_role="access",
    )
    return await access.mesh.issue(actor, code="1234")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["lifecycle", "message-events"])
async def test_interest_uses_shared_membership_after_join_and_leave(tmp_path, kind):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        source = await node(
            tmp_path,
            "source",
            ("source", "access"),
            routes,
            client,
            trusted_names=("source",),
        )
        access = await node(tmp_path, "access", ("source", "access"), routes, client)
        if kind == "lifecycle":
            await subscribe(access, "lifecycle")
            payload = await access.lifecycle.local_interest()
        else:
            slot = await issue_recipient(access)
            await message_subscription(access, slot)
            payload = await access.lifecycle.message_events.snapshot()
            payload = {**payload, "offset": 0, "complete": True}
        url = "https://source/internal/fleet/access-mesh/" + kind + "/interest"
        headers = {"X-Terminal-MCP-Peer": "access", "Authorization": "Bearer isolated-token"}
        assert (await client.post(url, json=payload, headers=headers)).status_code == 401
        await membership(source, ("source", "access"), authority="access")
        await membership(access, ("source", "access"), authority="access")
        await source.replication._snapshot(source.replication.peers[0])
        assert (await client.post(url, json=payload, headers=headers)).status_code == 200
        # A current member must still prove its credential; membership alone is insufficient.
        assert (
            await client.post(
                url, json=payload, headers={**headers, "Authorization": "Bearer incorrect"}
            )
        ).status_code == 401
        assert (
            await client.post(
                url, json=payload, headers={**headers, "X-Terminal-MCP-Peer": "outsider"}
            )
        ).status_code == 401
        await membership(source, ("source",))
        assert (await client.post(url, json=payload, headers=headers)).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("remote", [False, True], ids=["local", "remote-join"])
async def test_message_callback_envelope_and_remote_join_backfill(tmp_path, remote):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        source = await node(
            tmp_path,
            "source",
            ("source", "access"),
            routes,
            client,
            trusted_names=("source",),
        )
        access = await node(
            tmp_path,
            "access",
            ("source", "access"),
            routes,
            client,
            trusted_names=("access",),
        )
        slot = await issue_recipient(access)
        await message_subscription(access, slot)
        producer = source if remote else access
        wire = local_message(
            producer, slot["logical_agent_id"], "control", "Проверка сообщения 1/4."
        )
        local_message(producer, "another-recipient", "private-other", "must stay private")
        await producer.events.pump()
        if remote:
            assert records(source) == []
            await membership(source, ("source", "access"), authority="access")
            await membership(access, ("source", "access"), authority="access")
            await source.replication._snapshot(source.replication.peers[0])
            await access.lifecycle.sync_peer(access.replication.peers[0])
            await source.lifecycle.sync_peer(source.replication.peers[0])
        imported = records(access)
        assert len(imported) == 1 and imported[0][1] == "message.received"
        assert imported[0][4] == ("source" if remote else None)
        assert json.loads(imported[0][3])["message_id"] == wire["message_hash"]
        assert [r[0] for r in records(producer)] == [imported[0][0]]
        await access.events.pump()
        callbacks = []

        async def callback(url, body, headers):
            payload = json.loads(body)
            assert set(payload) == {"eventId", "name", "timestamp", "data"}
            assert payload["eventId"] == headers["webhook-id"] == imported[0][0]
            assert headers["X-MCP-Subscription-Id"] == "private"
            expected = hmac.new(
                base64.b64decode(SECRET[6:]),
                (headers["webhook-id"] + "." + headers["webhook-timestamp"] + ".").encode() + body,
                hashlib.sha256,
            ).digest()
            assert headers["webhook-signature"] == "v1," + base64.b64encode(expected).decode()
            assert payload["data"] == {
                "server": producer.events.node_id,
                "sender": "Bravo",
                "recipient": "Alpha",
                "message_id": wire["message_hash"],
                "text": wire["text"],
            }
            schema = next(
                event["payloadSchema"]
                for event in event_catalog()["events"]
                if event["name"] == "message.received"
            )
            jsonschema.validate(payload["data"], schema)
            assert payload["timestamp"] == wire["created_at"]
            callbacks.append(payload)
            return 200, b"{}"

        adapter = MCPEventsAdapter(None, access.events, sender=callback, access_checker=allowed)
        await adapter.deliver_due()
        await adapter.deliver_due()
        assert len(callbacks) == 1
        with sqlite3.connect(access.path) as db:
            assert db.execute(
                "SELECT state,attempts,last_status FROM mcp_event_deliveries"
            ).fetchall() == [("sent", 1, 200)]
        with sqlite3.connect(producer.path) as db:
            assert db.execute(
                "SELECT read_at,replied_at FROM coordination_message_recipients "
                "WHERE message_hash=?",
                (wire["message_hash"],),
            ).fetchall() == [(None, None)]
