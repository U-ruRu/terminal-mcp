"""Access-only message.received using existing issuer/attach identity."""

from __future__ import annotations

import base64
import json
import sqlite3

from fastapi.testclient import TestClient

from terminal_mcp.app import create_app
from terminal_mcp.config import Settings
from terminal_mcp.core.human_names import number_agent_name
from terminal_mcp.core.orchestration import utc_text
from terminal_mcp.mcp.message_binding import provider_metadata_from_events
from terminal_mcp.storage.access_mesh import public_name

KEY = "whsec_" + base64.b64encode(b"m" * 32).decode()
URL = "https://receiver.example.test/hooks"


def config(tmp_path):
    return Settings(
        database_path=tmp_path / "db.sqlite3",
        auth_database_path=tmp_path / "auth.sqlite3",
        output_cache_path=tmp_path / "output.sqlite3",
        runtime_config_path=tmp_path / "runtime.env",
        env_file_path=tmp_path / "terminal.env",
        log_path=tmp_path / "terminal.log",
        metrics_port=0,
        cwd=tmp_path,
        public_base_url="https://terminal.example",
        auth_mode="bearer",
        bearer_tokens="test-bearer-a,test-bearer-b",
        mcp_auth_mode="",
        actions_auth_mode="",
        persistent_agents_enabled=True,
        fleet_instance_id="firstbyte",
        fleet_id="",
        fleet_signing_private_key=base64.b64encode(bytes(range(32))).decode(),
        fleet_peers_json="[]",
        fleet_v1_source_enabled=False,
        fleet_v1_authority_enabled=False,
        fleet_v1_projection_enabled=False,
        fleet_v1_public_enabled=False,
        access_mesh_enabled=True,
        access_mesh_proof_key="safe-local-test-mesh-key-not-production",
        access_mesh_peers_json="[]",
    )


def meta(subject="a-user", conversation="a-session"):
    return {"openai/subject": subject, "openai/session": conversation}


def rpc(client, role, method, params=None, *, evidence=None, token="test-bearer-a", request_id=1):
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": dict(params or {}),
    }
    if evidence is not None:
        payload["params"]["_meta"] = evidence
    response = client.post(
        f"/terminal-mcp/{role}/v1/mcp/",
        json=payload,
        headers={
            "authorization": "Bearer " + token,
            "accept": "application/json, text/event-stream",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def tool(client, role, name, args, *, evidence, token="test-bearer-a", request_id=1):
    response = rpc(
        client,
        role,
        "tools/call",
        {"name": name, "arguments": args},
        evidence=evidence,
        token=token,
        request_id=request_id,
    )
    assert "result" in response, response
    result = response["result"]
    assert result["isError"] is False, result
    return result.get("structuredContent") or json.loads(result["content"][0]["text"])


class Receiver:
    def __init__(self):
        self.requests = []

    async def __call__(self, url, raw_body, headers):
        assert url == URL
        data = json.loads(raw_body)
        self.requests.append((data, headers))
        if data.get("type") == "verification":
            return 200, json.dumps({"challenge": data["challenge"]}).encode()
        return 202, b""


def subscribe(client, *, evidence, token="test-bearer-a"):
    return rpc(
        client,
        "access",
        "events/subscribe",
        {
            "name": "message.received",
            "arguments": {},
            "delivery": {"mode": "webhook", "url": URL, "secret": KEY},
        },
        evidence=evidence,
        token=token,
    )


def test_metadata_extraction_exact_and_missing():
    assert provider_metadata_from_events({"_meta": meta()}) == meta()
    assert provider_metadata_from_events({"_meta": {"openai/subject": "user"}}) is None
    assert provider_metadata_from_events({"arguments": meta()}) is None
    assert provider_metadata_from_events({}) is None
    assert provider_metadata_from_events(
        {"_meta": {"threadId": "thread-a", "progressToken": 2}},
        principal_id="verified-client",
    ) == {"threadId": "thread-a"}
    assert provider_metadata_from_events({"_meta": {"threadId": "thread-a"}}) is None
    assert provider_metadata_from_events({"_meta": {"sessionId": "root"}}) is None


def test_access_subscription_without_verified_binding_is_fail_closed(tmp_path):
    app = create_app(config(tmp_path))
    receiver = Receiver()
    app.state.access_events.sender = receiver
    with TestClient(app, base_url="https://terminal.example") as client:
        assert (
            "events"
            in rpc(
                client,
                "access",
                "server/discover",
                {},
                evidence=meta(),
            )["result"]["capabilities"]
        )
        for role in ("executor", "coordinator"):
            assert (
                "events"
                not in rpc(
                    client,
                    role,
                    "server/discover",
                    {},
                    evidence=meta(),
                )["result"]["capabilities"]
            )
        unknown = subscribe(client, evidence=meta())
        assert unknown["error"]["code"] == -32001
        assert receiver.requests == []  # reject BEFORE external challenge
        missing = subscribe(client, evidence=None)
        assert missing["error"]["code"] == -32001
        with sqlite3.connect(app.state.settings.database_path) as db:
            assert db.execute("SELECT COUNT(*) FROM mcp_event_subscriptions").fetchone()[0] == 0


def test_access_issue_other_connector_attach_subscribe_and_local_delivery(tmp_path):
    app = create_app(config(tmp_path))
    receiver = Receiver()
    app.state.access_events.sender = receiver
    app.state.mcp_events.sender = receiver
    with TestClient(app, base_url="https://terminal.example") as client:
        a_access = meta("subject-access-a", "conversation-access-a")
        a_coord = meta("subject-coord-a", "conversation-coord-a")
        b_access = meta("subject-access-b", "conversation-access-b")
        b_coord = meta("subject-coord-b", "conversation-coord-b")
        a_number = tool(
            client,
            "access",
            "session",
            {"action": "start"},
            evidence=a_access,
            request_id=11,
        )["session_number"]
        a_slot = app.state.access_mesh.store.numbers.winner(a_number)
        a_id = a_slot["logical_agent_id"]
        assert tool(
            client,
            "coordinator",
            "session",
            {"session_number": a_number, "intent": "Receive fixture message"},
            evidence=a_coord,
            request_id=12,
        )["ok"]
        # Access has DIFFERENT subject and conversation from Coordination,
        # nevertheless both existing bindings refer to the exact same agent.
        result = subscribe(client, evidence=a_access)
        assert "result" in result, result
        subscription_id = result["result"]["id"]
        with sqlite3.connect(app.state.settings.database_path) as db:
            binding = db.execute(
                "SELECT recipient_agent_id,recipient_role FROM mcp_event_message_bindings "
                "WHERE subscription_id=?",
                (subscription_id,),
            ).fetchone()
            assert binding == (a_id, "access")
        assert len(receiver.requests) == 1  # challenge only
        b_number = tool(
            client,
            "access",
            "session",
            {"action": "start"},
            evidence=b_access,
            request_id=21,
        )["session_number"]
        b_slot = app.state.access_mesh.store.numbers.winner(b_number)
        b_id = b_slot["logical_agent_id"]
        assert b_id != a_id
        tool(
            client,
            "coordinator",
            "session",
            {"session_number": b_number, "intent": "Send fixture message"},
            evidence=b_coord,
            request_id=22,
        )
        with sqlite3.connect(app.state.settings.database_path) as db:
            db.execute(
                "UPDATE logical_agents SET display_name='Reused name' "
                "WHERE logical_agent_id IN (?,?)",
                (a_id, b_id),
            )
        b_sub = subscribe(client, evidence=b_access)
        assert "result" in b_sub, b_sub
        assert b_sub["result"]["id"] != subscription_id
        with sqlite3.connect(app.state.settings.database_path) as db:
            assert db.execute(
                "SELECT recipient_agent_id FROM mcp_event_message_bindings WHERE subscription_id=?",
                (b_sub["result"]["id"],),
            ).fetchone() == (b_id,)
        sent = tool(
            client,
            "coordinator",
            "message",
            {
                "action": "send",
                "scope": "local",
                "mode": "notify",
                "target": public_name(a_slot["issuer_id"], a_id),
                "text": "private message to A only",
            },
            evidence=b_coord,
            request_id=23,
        )
        assert sent["ok"], sent
        with sqlite3.connect(app.state.settings.database_path) as db:
            message_id = db.execute(
                "SELECT message_hash FROM coordination_messages "
                "WHERE text='private message to A only' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()[0]
        # Run the actual durable producer/delivery worker on isolated SQLite.
        client.portal.call(app.state.mcp_events.store.pump)
        client.portal.call(app.state.mcp_events.deliver_due)
        event_payloads = [
            data for data, _ in receiver.requests if data.get("name") == "message.received"
        ]
        assert len(event_payloads) == 1, event_payloads
        delivered = event_payloads[0]
        assert delivered["data"]["server"] == "firstbyte"
        assert delivered["data"]["message_id"] == message_id
        assert delivered["data"]["text"] == "private message to A only"
        assert delivered["data"]["sender"] and delivered["data"]["recipient"]
        assert delivered["timestamp"]
        assert "timestamp" not in delivered["data"]
        with sqlite3.connect(app.state.settings.database_path) as db:
            status = db.execute(
                "SELECT read_at,replied_at,seen_count FROM coordination_message_recipients "
                "WHERE message_hash=? AND recipient_agent_id=?",
                (message_id, a_id),
            ).fetchone()
            assert status == (None, None, 0)
            delivered_rows = db.execute(
                "SELECT event_id FROM mcp_event_records WHERE name='message.received'"
            ).fetchall()
            assert delivered_rows == [(delivered["eventId"],)]
            db.execute(
                "UPDATE coordination_message_recipients SET read_at=? "
                "WHERE message_hash=? AND recipient_agent_id=?",
                (utc_text(), message_id, a_id),
            )
        client.portal.call(app.state.mcp_events.store.pump)
        client.portal.call(app.state.mcp_events.deliver_due)
        assert (
            len([data for data, _ in receiver.requests if data.get("name") == "message.received"])
            == 1
        )
        tool(
            client,
            "coordinator",
            "message",
            {
                "action": "send",
                "scope": "local",
                "mode": "notify",
                "target": public_name(b_slot["issuer_id"], b_id),
                "text": "private message to B only",
            },
            evidence=a_coord,
            request_id=24,
        )
        client.portal.call(app.state.mcp_events.store.pump)
        client.portal.call(app.state.mcp_events.deliver_due)
        message_events = [
            data for data, _ in receiver.requests if data.get("name") == "message.received"
        ]
        assert len(message_events) == 2
        assert [event["data"]["text"] for event in message_events] == [
            "private message to A only",
            "private message to B only",
        ]
        assert message_events[0]["eventId"] != message_events[1]["eventId"]
        assert message_events[0]["data"]["recipient"] == number_agent_name(a_number)
        assert message_events[1]["data"]["recipient"] == number_agent_name(b_number)
        with sqlite3.connect(app.state.settings.database_path) as db:
            assert db.execute(
                "SELECT COUNT(DISTINCT recipient_agent_id) "
                "FROM mcp_event_records WHERE name='message.received'"
            ).fetchone() == (2,)
            # Emulate a durable bounded retry without recreating the message.
            db.execute(
                "UPDATE mcp_event_deliveries SET state='pending',"
                "next_attempt_at=? WHERE event_id=? AND subscription_id=?",
                (utc_text(), message_events[0]["eventId"], subscription_id),
            )
        client.portal.call(app.state.mcp_events.deliver_due)
        retried = [data for data, _ in receiver.requests if data.get("name") == "message.received"]
        assert len(retried) == 3
        assert retried[-1]["eventId"] == message_events[0]["eventId"]
        assert retried[-1]["data"]["message_id"] == message_id
        # A new OAuth-bearing connector identity with the same public label
        # cannot consume the A session without having the exact Access binding.
        denied = subscribe(client, evidence=meta("other-user", "other-conversation"))
        assert denied["error"]["code"] == -32001


def test_token_boundary_cannot_reuse_existing_metadata_pair(tmp_path):
    app = create_app(config(tmp_path))
    app.state.access_events.sender = Receiver()
    with TestClient(app, base_url="https://terminal.example") as client:
        evidence = meta("unique-subject", "unique-conversation")
        number = tool(
            client,
            "access",
            "session",
            {"action": "start"},
            evidence=evidence,
            request_id=1,
        )["session_number"]
        assert number
        # Different authenticated OAuth/bearer principal, identical metadata,
        # must produce a different connection key and is not authorized.
        denied = subscribe(client, evidence=evidence, token="test-bearer-b")
        assert denied["error"]["code"] == -32001


def test_oauth_access_binding_subscribe_and_revocation(tmp_path):
    import time

    import jwt

    base = config(tmp_path)
    values = base.model_dump()
    values.update(
        auth_mode="oauth",
        oauth_issuer="https://terminal.example",
        oauth_audience="https://terminal.example/mcp",
        oauth_signing_secret="test-oauth-signing-secret-of-32-bytes!",
    )
    app = create_app(Settings(**values))
    receiver = Receiver()
    app.state.access_events.sender = receiver
    with TestClient(app, base_url="https://terminal.example") as client:
        cid, _ = client.portal.call(
            app.state.oauth_store.register_client,
            ["https://chat.example/callback"],
            "Fixture",
            "none",
        )
        now = int(time.time())
        jwt_token = jwt.encode(
            {
                "iss": "https://terminal.example",
                "aud": "https://terminal.example/mcp",
                "sub": cid,
                "iat": now,
                "exp": now + 3600,
                "scope": "terminal:read terminal:execute",
            },
            values["oauth_signing_secret"],
            algorithm="HS256",
        )
        pair = meta("oauth-user", "oauth-access-conversation")
        number = tool(
            client,
            "access",
            "session",
            {"action": "start"},
            evidence=pair,
            token=jwt_token,
            request_id=77,
        )["session_number"]
        assert number
        saved = subscribe(client, evidence=pair, token=jwt_token)
        assert "result" in saved, saved
        sub_id = saved["result"]["id"]
        assert len(receiver.requests) == 1
        with sqlite3.connect(app.state.settings.database_path) as db:
            agent, credential = db.execute(
                "SELECT b.recipient_agent_id,s.credential_id "
                "FROM mcp_event_subscriptions s "
                "JOIN mcp_event_message_bindings b ON b.subscription_id=s.id "
                "WHERE s.id=?",
                (sub_id,),
            ).fetchone()
        assert agent and credential == "oauth:" + cid
        # A valid OAuth client is part of each delivery authorization check.
        item = {
            "name": "message.received",
            "credential_id": credential,
            "auth_mode": "oauth",
            "recipient_agent_id": agent,
        }
        with sqlite3.connect(app.state.settings.database_path) as db:
            fields = db.execute(
                "SELECT recipient_session_id,recipient_epoch,recipient_connection_key,"
                "recipient_node_id,recipient_issuer_id,recipient_slot_id,recipient_role "
                "FROM mcp_event_message_bindings WHERE subscription_id=?",
                (sub_id,),
            ).fetchone()
        item.update(
            dict(
                zip(
                    (
                        "recipient_session_id",
                        "recipient_epoch",
                        "recipient_connection_key",
                        "recipient_node_id",
                        "recipient_issuer_id",
                        "recipient_slot_id",
                        "recipient_role",
                    ),
                    fields,
                    strict=True,
                )
            )
        )
        assert client.portal.call(app.state.mcp_events.access_checker, item)
        client.portal.call(app.state.oauth_store.delete_client, cid)
        assert not client.portal.call(app.state.mcp_events.access_checker, item)


def test_codex_native_thread_only_reuses_verified_identity_and_isolates_clients(tmp_path):
    """One native Codex thread works across Access, Coordinator and MCP Events.

    A different authenticated client cannot claim the prior Access/attach binding.
    This is a live HTTP/MCP integration case rather than a metadata parser test.
    """
    app = create_app(config(tmp_path))
    receiver = Receiver()
    app.state.access_events.sender = receiver
    with TestClient(app, base_url="https://terminal.example") as client:
        native_meta = {"threadId": "codex-thread-a", "progressToken": 14}
        issued = tool(client, "access", "session", {"action": "start"}, evidence=native_meta)
        number = issued["session_number"]
        slot = app.state.access_mesh.store.numbers.winner(number)
        logical_agent_id = slot["logical_agent_id"]
        attached = tool(
            client,
            "coordinator",
            "session",
            {"session_number": number, "intent": "Native Codex metadata test"},
            evidence={"threadId": "codex-thread-a"},
            request_id=55,
        )
        assert attached["ok"]
        resolved = tool(
            client,
            "coordinator",
            "health",
            {"extended": False},
            evidence={"threadId": "codex-thread-a", "callId": "ignored-call"},
            request_id=56,
        )
        assert resolved["ok"] and resolved["agent_name"]

        subscribed = subscribe(client, evidence=native_meta)
        assert "result" in subscribed, subscribed
        with sqlite3.connect(app.state.settings.database_path) as db:
            binding = db.execute(
                "SELECT recipient_agent_id FROM mcp_event_message_bindings WHERE subscription_id=?",
                (subscribed["result"]["id"],),
            ).fetchone()
        assert binding == (logical_agent_id,)

        # Same thread from another verified OAuth client has a distinct connection.
        other_principal = subscribe(
            client, evidence={"threadId": "codex-thread-a"}, token="test-bearer-b"
        )
        assert "error" in other_principal
        assert other_principal["error"]["code"] == -32001

        # Root session alone cannot identify which child agent sent a request.
        missing_thread = subscribe(
            client, evidence={"sessionId": "shared-root", "progressToken": 42}
        )
        assert "error" in missing_thread
        assert missing_thread["error"]["code"] == -32001



def test_access_lifecycle_subscriptions_and_get_events_use_logical_agent_identity(tmp_path):
    app = create_app(config(tmp_path))
    receiver = Receiver()
    app.state.access_events.sender = receiver
    with TestClient(app, base_url="https://terminal.example") as client:
        agent_a, agent_b = meta("owner-a", "conversation-a"), meta("owner-b", "conversation-b")
        a_code = tool(client, "access", "session", {"action": "start"}, evidence=agent_a)[
            "session_number"
        ]
        b_code = tool(client, "access", "session", {"action": "start"}, evidence=agent_b)[
            "session_number"
        ]
        store = app.state.access_mesh.store
        a_number = store.numbers.winner(a_code)
        b_number = store.numbers.winner(b_code)
        a_id = store.slot(a_number["issuer_id"], a_number["slot_id"]).logical_agent_id
        b_id = store.slot(b_number["issuer_id"], b_number["slot_id"]).logical_agent_id
        assert a_id != b_id

        def subscribe_event(event_name, evidence):
            response = rpc(
                client, "access", "events/subscribe",
                {"name": event_name, "arguments": {},
                 "delivery": {"mode": "webhook", "url": URL, "secret": KEY}},
                evidence=evidence,
            )
            assert "result" in response, response
            return response["result"]["id"]

        sub_a = subscribe_event("agent.attached", agent_a)
        sub_b = subscribe_event("session.ended", agent_b)
        unauth = rpc(
            client, "access", "events/subscribe",
            {"name": "agent.inactive", "arguments": {},
             "delivery": {"mode": "webhook", "url": URL, "secret": KEY}},
            evidence=meta("not-registered", "elsewhere"),
        )
        assert unauth["error"]["code"] == -32001

        a_event, b_event = "evt_" + "a" * 32, "evt_" + "b" * 32
        path = app.state.mcp_events.store.path
        timestamp = utc_text()
        with sqlite3.connect(path) as db:
            assert db.execute(
                "SELECT subscriber_agent_id FROM mcp_event_subscriptions WHERE id=?",
                (sub_a,),
            ).fetchone() == (a_id,)
            assert db.execute(
                "SELECT subscriber_agent_id FROM mcp_event_subscriptions WHERE id=?",
                (sub_b,),
            ).fetchone() == (b_id,)
            for event_id, event_name, subscriber, name in (
                (a_event, "agent.attached", sub_a, "A"),
                (b_event, "session.ended", sub_b, "B"),
            ):
                data = {"server": "firstbyte", "agent_name": name}
                db.execute(
                    "INSERT INTO mcp_event_records("
                    "event_id,source_key,name,occurred_at,data_json) VALUES(?,?,?,?,?)",
                    (event_id, "test:" + event_id, event_name,
                     timestamp, json.dumps(data)),
                )
                db.execute(
                    "INSERT INTO mcp_event_deliveries("
                    "event_id,subscription_id,state,next_attempt_at) VALUES(?,?,?,?)",
                    (event_id, subscriber, "sent", timestamp),
                )

        first = tool(client, "access", "get_events", {}, evidence=agent_a)
        assert first["ok"] and [item["eventId"] for item in first["events"]] == [a_event]
        assert first["events"][0]["data"]["agent_name"] == "A"
        assert first["events"][0]["read"] is False
        assert tool(client, "access", "get_events", {}, evidence=agent_a)["events"] == []
        assert [item["eventId"] for item in tool(
            client, "access", "get_events", {}, evidence=agent_b
        )["events"]] == [b_event]
        assert tool(client, "access", "get_events",
                    {"event_id": a_event}, evidence=agent_b)["events"] == []
        by_id = tool(client, "access", "get_events",
                     {"event_id": a_event}, evidence=agent_a)
        assert by_id["events"][0]["read"] is True
        old = tool(client, "access", "get_events",
                   {"show_read": True}, evidence=agent_a)
        assert [item["eventId"] for item in old["events"]] == [a_event]
        with sqlite3.connect(path) as db:
            assert db.execute(
                "SELECT state,read_at FROM mcp_event_deliveries WHERE event_id=?",
                (a_event,),
            ).fetchone()[0] == "sent"
            assert db.execute(
                "SELECT read_at FROM mcp_event_deliveries WHERE event_id=?",
                (a_event,),
            ).fetchone()[0] is not None


def test_access_get_events_cursor_deduplicates_across_owner_subscriptions(tmp_path):
    app = create_app(config(tmp_path))

    class MultiCallbackReceiver:
        async def __call__(self, url, raw_body, headers):
            assert url in (URL, URL + "/second")
            payload = json.loads(raw_body)
            if payload.get("type") == "verification":
                return 200, json.dumps({"challenge": payload["challenge"]}).encode()
            return 202, b""

    app.state.access_events.sender = MultiCallbackReceiver()
    evidence = meta("owner", "same-conversation")
    with TestClient(app, base_url="https://terminal.example") as client:
        tool(client, "access", "session", {"action": "start"}, evidence=evidence)
        payload = {
            "name": "agent.attached", "arguments": {},
            "delivery": {"mode": "webhook", "url": URL, "secret": KEY}
        }
        response = rpc(client, "access", "events/subscribe", payload, evidence=evidence)
        assert "result" in response, response
        sub_id = response["result"]["id"]
        secondary = rpc(
            client, "access", "events/subscribe",
            {
                "name": "agent.attached", "arguments": {},
                "delivery": {"mode": "webhook", "url": URL + "/second", "secret": KEY},
            },
            evidence=evidence,
        )
        assert "result" in secondary, secondary
        second_sub_id = secondary["result"]["id"]
        assert second_sub_id != sub_id
        path = app.state.mcp_events.store.path
        event_ids = ["evt_" + digit * 32 for digit in "cde"]
        with sqlite3.connect(path) as db:
            for index, event_id in enumerate(event_ids):
                db.execute(
                    "INSERT INTO mcp_event_records("
                    "event_id,source_key,name,occurred_at,data_json) VALUES(?,?,?,?,?)",
                    (event_id, "test:" + event_id, "agent.attached",
                     f"2026-10-14T02:00:0{index}Z",
                     json.dumps({"server": "firstbyte", "agent_name": str(index)})),
                )
                db.execute(
                    "INSERT INTO mcp_event_deliveries("
                    "event_id,subscription_id,next_attempt_at) VALUES(?,?,?)",
                    (event_id, sub_id, utc_text()),
                )
            # Same event has two independent subscriptions owned by one agent.
            db.execute(
                "INSERT INTO mcp_event_deliveries("
                "event_id,subscription_id,next_attempt_at) VALUES(?,?,?)",
                (event_ids[0], second_sub_id, utc_text()),
            )
        first = tool(client, "access", "get_events", {"limit": 1}, evidence=evidence)
        assert [x["eventId"] for x in first["events"]] == event_ids[:1]
        with sqlite3.connect(path) as db:
            assert db.execute(
                "SELECT COUNT(*) FROM mcp_event_deliveries "
                "WHERE event_id=? AND read_at IS NOT NULL",
                (event_ids[0],),
            ).fetchone()[0] == 2
        assert first["has_more"] and first["next_cursor"]
        second = tool(client, "access", "get_events",
                      {"limit": 1, "cursor": first["next_cursor"]}, evidence=evidence)
        assert [x["eventId"] for x in second["events"]] == event_ids[1:2]
        third = tool(client, "access", "get_events",
                     {"limit": 1, "cursor": second["next_cursor"]}, evidence=evidence)
        assert [x["eventId"] for x in third["events"]] == event_ids[2:]
        assert third["has_more"] is False
        assert tool(client, "access", "get_events", {}, evidence=evidence)["events"] == []


def test_number_subscription_owner_is_caller_not_message_recipient(tmp_path):
    """Routing to a recipient by session number must not replace the subscriber."""
    app = create_app(config(tmp_path))
    app.state.access_events.sender = Receiver()
    evidence_a = meta("owner-a", "session-a")
    evidence_b = meta("owner-b", "session-b")
    with TestClient(app, base_url="https://terminal.example") as client:
        number_a = tool(client, "access", "session", {"action": "start"}, evidence=evidence_a)[
            "session_number"
        ]
        number_b = tool(client, "access", "session", {"action": "start"}, evidence=evidence_b)[
            "session_number"
        ]
        store = app.state.access_mesh.store
        a_number = store.numbers.winner(number_a)
        b_number = store.numbers.winner(number_b)
        id_a = store.slot(a_number["issuer_id"], a_number["slot_id"]).logical_agent_id
        id_b = store.slot(b_number["issuer_id"], b_number["slot_id"]).logical_agent_id
        assert id_a != id_b
        response = rpc(
            client,
            "access",
            "events/subscribe",
            {
                "name": "message.received",
                "arguments": {"session_number": number_a},
                "delivery": {"mode": "webhook", "url": URL, "secret": KEY},
            },
            evidence=evidence_b,
        )
        assert "result" in response, response
        sub_id = response["result"]["id"]
        with sqlite3.connect(app.state.mcp_events.store.path) as db:
            row = db.execute(
                "SELECT s.subscriber_agent_id,b.recipient_agent_id "
                "FROM mcp_event_subscriptions s JOIN mcp_event_message_bindings b "
                "ON b.subscription_id=s.id WHERE s.id=?",
                (sub_id,),
            ).fetchone()
        assert row == (id_b, id_a)
        # An old number-based subscription whose owner field was not stored
        # must not be reassigned to the recipient during schema backfill.
        with sqlite3.connect(app.state.mcp_events.store.path) as db:
            db.execute(
                "UPDATE mcp_event_subscriptions SET subscriber_agent_id=NULL WHERE id=?",
                (sub_id,),
            )
        client.portal.call(app.state.mcp_events.store.initialize)
        with sqlite3.connect(app.state.mcp_events.store.path) as db:
            assert db.execute(
                "SELECT subscriber_agent_id FROM mcp_event_subscriptions WHERE id=?",
                (sub_id,),
            ).fetchone() == (None,)
