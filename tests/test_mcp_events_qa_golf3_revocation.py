"""Independent exact-SHA security acceptance probes; all data disposable."""

from __future__ import annotations

import base64
import json
import sqlite3
import time

import jwt
from fastapi.testclient import TestClient

from terminal_mcp.app import create_app
from terminal_mcp.config import Settings

URL = "https://qa-receiver.example.test/webhook"
SECRET = "whsec_" + base64.b64encode(b"Q" * 32).decode()
ISSUER = "https://qa.terminal.example"
SIGNER = "independent-golf3-only-oauth-signing-key!"


class CallbackSink:
    def __init__(self):
        self.calls = []
        self.frames = []

    async def __call__(self, url, body, headers):
        assert url == URL
        payload = json.loads(body)
        self.calls.append(payload)
        self.frames.append((body, headers))
        if payload.get("type") == "verification":
            return 200, json.dumps({"challenge": payload["challenge"]}).encode()
        return 202, b""


def settings(tmp_path):
    return Settings(
        database_path=tmp_path / "local.sqlite3",
        auth_database_path=tmp_path / "oauth.sqlite3",
        output_cache_path=tmp_path / "output.sqlite3",
        runtime_config_path=tmp_path / "runtime.env",
        env_file_path=tmp_path / "application.env",
        log_path=tmp_path / "application.log",
        metrics_port=0,
        cwd=tmp_path,
        public_base_url=ISSUER,
        auth_mode="oauth",
        oauth_issuer=ISSUER,
        oauth_audience=ISSUER + "/mcp",
        oauth_signing_secret=SIGNER,
        persistent_agents_enabled=True,
        fleet_instance_id="secondary-qa",
        fleet_id="",
        fleet_signing_private_key=base64.b64encode(bytes(range(32))).decode(),
        fleet_peers_json="[]",
        fleet_v1_source_enabled=False,
        fleet_v1_authority_enabled=False,
        fleet_v1_projection_enabled=False,
        fleet_v1_public_enabled=False,
        access_mesh_enabled=True,
        access_mesh_proof_key="independent-qa-only-unique-non-production",
        access_mesh_peers_json="[]",
    )


def call(client, role, method, token, metadata, **params):
    return client.post(
        f"/terminal-mcp/{role}/v1/mcp/",
        json={
            "jsonrpc": "2.0",
            "id": 778,
            "method": method,
            "params": {**params, "_meta": metadata},
        },
        headers={
            "Authorization": "Bearer " + token,
            "accept": "application/json, text/event-stream",
        },
    ).json()


def provision(client, app):
    client_id, _ = client.portal.call(
        app.state.oauth_store.register_client,
        ["https://connector.example.test/complete"],
        "QA only",
        "none",
    )
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": ISSUER + "/mcp",
            "sub": client_id,
            "iat": now,
            "exp": now + 3600,
            "scope": "terminal:read terminal:execute",
        },
        SIGNER,
        algorithm="HS256",
    )
    evidence = {"openai/subject": "qa-oauth-subject", "openai/session": "qa-conversation"}
    session = call(
        client,
        "access",
        "tools/call",
        token,
        evidence,
        name="session",
        arguments={"action": "start"},
    )
    assert "result" in session, session
    assert not session["result"]["isError"], session
    result = session["result"].get("structuredContent")
    if result is None:
        result = json.loads(session["result"]["content"][0]["text"])
    slot = app.state.access_mesh.store.numbers.winner(result["session_number"])
    assert slot is not None
    return client_id, token, evidence, slot


def subscribe(client, token, evidence):
    return call(
        client,
        "access",
        "events/subscribe",
        token,
        evidence,
        name="message.received",
        arguments={},
        delivery={"mode": "webhook", "url": URL, "secret": SECRET},
    )


def suspend_slot(app, slot):
    """Emulate an Access Mesh slot revocation in disposable test DB only."""
    with sqlite3.connect(app.state.settings.database_path) as db:
        cur = db.execute(
            "UPDATE access_mesh_slot_replicas SET state='suspended' "
            "WHERE issuer_id=? AND slot_id=?",
            (slot["issuer_id"], slot["slot_id"]),
        )
        assert cur.rowcount == 1


def test_suspended_access_slot_blocks_new_message_subscription(tmp_path):
    app = create_app(settings(tmp_path))
    sink = CallbackSink()
    app.state.access_events.sender = sink
    with TestClient(app, base_url=ISSUER) as client:
        _, token, evidence, slot = provision(client, app)
        suspend_slot(app, slot)
        result = subscribe(client, token, evidence)
        assert "error" in result, result
        assert sink.calls == []  # strict authorization before outbound callback challenge
        with sqlite3.connect(app.state.settings.database_path) as db:
            assert db.execute("SELECT COUNT(*) FROM mcp_event_subscriptions").fetchone()[0] == 0


def test_suspension_blocks_pending_callback_and_deletes_subscription(tmp_path):
    """Test the outbound path, not merely a mocked checker boolean."""
    from terminal_mcp.core.orchestration import utc_text

    app = create_app(settings(tmp_path))
    sink = CallbackSink()
    app.state.access_events.sender = sink
    with TestClient(app, base_url=ISSUER) as client:
        _, token, evidence, slot = provision(client, app)
        saved = subscribe(client, token, evidence)
        assert "result" in saved, saved
        sid = saved["result"]["id"]
        assert len(sink.calls) == 1 and sink.calls[0]["type"] == "verification"
        with sqlite3.connect(app.state.settings.database_path) as db:
            stamp = utc_text()
            db.execute(
                "INSERT INTO mcp_event_records("
                "event_id,source_key,name,occurred_at,data_json,recipient_agent_id,namespace)"
                " VALUES(?,?,?,?,?,?,NULL)",
                (
                    "evt_independent_qa",
                    "qa-only-independent-source",
                    "message.received",
                    stamp,
                    json.dumps(
                        {
                            "server": "secondary-qa",
                            "sender": "Fixture",
                            "recipient": "Fixture",
                            "message_id": "qa-only-message",
                            "text": "sensitive local fixture",
                            "timestamp": stamp,
                        }
                    ),
                    slot["logical_agent_id"],
                ),
            )
            db.execute(
                "INSERT INTO mcp_event_deliveries("
                "event_id,subscription_id,next_attempt_at) VALUES(?,?,?)",
                ("evt_independent_qa", sid, stamp),
            )
        suspend_slot(app, slot)
        client.portal.call(app.state.access_events.deliver_due)
        assert len(sink.calls) == 1, "revoked slot must never receive signed callback"
        with sqlite3.connect(app.state.settings.database_path) as db:
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM mcp_event_subscriptions WHERE id=?", (sid,)
                ).fetchone()[0]
                == 0
            )


def test_revocation_during_challenge_must_not_save_private_subscription(tmp_path):
    """Revoke Access slot while verification is in flight; inspect commit decision."""
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url=ISSUER) as client:
        _, token, evidence, slot = provision(client, app)

        class RevokeAtVerification(CallbackSink):
            async def __call__(self, url, body, headers):
                payload = json.loads(body)
                if payload.get("type") == "verification":
                    suspend_slot(app, slot)
                return await super().__call__(url, body, headers)

        sink = RevokeAtVerification()
        app.state.access_events.sender = sink
        result = subscribe(client, token, evidence)
        assert "error" in result, (
            "BLOCKER: private subscription saved after revocation during callback verification",
            result,
        )
        with sqlite3.connect(app.state.settings.database_path) as db:
            assert db.execute("SELECT COUNT(*) FROM mcp_event_message_bindings").fetchone()[0] == 0


def test_native_access_attach_message_and_signed_callback_independent(tmp_path):
    """Exercise real ASGI Access+Coordinator paths with DISTINCT provider metadata."""
    import hashlib
    import hmac

    from terminal_mcp.core.orchestration import utc_text
    from terminal_mcp.storage.access_mesh import public_name

    app = create_app(settings(tmp_path))
    sink = CallbackSink()
    app.state.access_events.sender = sink
    with TestClient(app, base_url=ISSUER) as client:
        _, token, a_pair, a_slot = provision(client, app)
        a_id = a_slot["logical_agent_id"]
        client.portal.call(app.state.mcp_events.store.pump)
        with sqlite3.connect(app.state.settings.database_path) as db:
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM mcp_event_records WHERE name='agent.attached'"
                ).fetchone()[0]
                == 0
            ), "Access issuance must not masquerade as tool attach"
        a_coord = {"openai/subject": "qa-coordinate-A", "openai/session": "qa-work-A"}
        a_number = app.state.access_mesh.store.numbers.number_for_slot(
            a_slot["issuer_id"], a_slot["slot_id"]
        )
        attached = call(
            client,
            "coordinator",
            "tools/call",
            token,
            a_coord,
            name="session",
            arguments={"session_number": a_number, "intent": "Receive fixture message"},
        )
        assert "result" in attached and not attached["result"]["isError"], attached
        client.portal.call(app.state.mcp_events.store.pump)
        with sqlite3.connect(app.state.settings.database_path) as db:
            attached_rows = db.execute(
                "SELECT data_json FROM mcp_event_records WHERE name='agent.attached'"
            ).fetchall()
        assert len(attached_rows) == 1
        attached_data = json.loads(attached_rows[0][0])
        assert attached_data["server"] == "secondary-qa"
        assert attached_data["agent_name"]

        b_pair = {"openai/subject": "qa-access-B", "openai/session": "qa-access-conv-B"}
        b_start = call(
            client,
            "access",
            "tools/call",
            token,
            b_pair,
            name="session",
            arguments={"action": "start"},
        )
        assert "result" in b_start and not b_start["result"]["isError"], b_start
        b_content = b_start["result"].get("structuredContent")
        if b_content is None:
            b_content = json.loads(b_start["result"]["content"][0]["text"])
        b_slot = app.state.access_mesh.store.numbers.winner(b_content["session_number"])
        b_coord = {"openai/subject": "qa-coordinate-B", "openai/session": "qa-work-B"}
        b_attach = call(
            client,
            "coordinator",
            "tools/call",
            token,
            b_coord,
            name="session",
            arguments={
                "session_number": b_content["session_number"], "intent": "Send fixture message",
            },
        )
        assert "result" in b_attach and not b_attach["result"]["isError"], b_attach
        assert b_slot["logical_agent_id"] != a_id
        saved = subscribe(client, token, a_pair)
        assert "result" in saved, saved

        text_value = "private-QA-case: Unicode αβ, newline\\nseparate"
        sent = call(
            client,
            "coordinator",
            "tools/call",
            token,
            b_coord,
            name="message",
            arguments={
                "action": "send",
                "scope": "local",
                "mode": "notify",
                "target": public_name(a_slot["issuer_id"], a_id),
                "text": text_value,
            },
        )
        assert "result" in sent and not sent["result"]["isError"], sent
        sent_content = sent["result"].get("structuredContent") or json.loads(
            sent["result"]["content"][0]["text"]
        )
        assert sent_content["ok"] and sent_content["message"]["message_hash"], sent_content
        client.portal.call(app.state.mcp_events.store.pump)
        client.portal.call(app.state.access_events.deliver_due)
        events = [p for p in sink.calls if p.get("name") == "message.received"]
        assert len(events) == 1, events
        event = events[0]
        assert event["data"]["text"] == text_value
        assert event["data"]["server"] == "secondary-qa"
        assert event["data"]["message_id"].startswith("secondary-qa:localmsg:")
        assert event["data"]["timestamp"] and event["data"]["recipient"]
        assert event["data"]["sender"]
        frames = [(b, h) for b, h in sink.frames if json.loads(b).get("name") == "message.received"]
        assert len(frames) == 1
        body, headers = frames[0]
        signed = (
            headers["webhook-id"].encode()
            + b"."
            + headers["webhook-timestamp"].encode()
            + b"."
            + body
        )
        expected = base64.b64encode(
            hmac.new(
                base64.b64decode(SECRET.removeprefix("whsec_")), signed, hashlib.sha256
            ).digest()
        ).decode()
        assert headers["webhook-signature"].split(" ")[0] == "v1," + expected

        with sqlite3.connect(app.state.settings.database_path) as db:
            status_before = db.execute(
                "SELECT read_at,replied_at,seen_count FROM coordination_message_recipients "
                "WHERE message_hash=? AND recipient_agent_id=?",
                (event["data"]["message_id"], a_id),
            ).fetchone()
            assert status_before == (None, None, 0)
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM mcp_event_records WHERE name='message.received'"
                ).fetchone()[0]
                == 1
            )
            db.execute(
                "UPDATE mcp_event_deliveries SET state='pending',next_attempt_at=? "
                "WHERE event_id=?",
                (utc_text(), event["eventId"]),
            )
        client.portal.call(app.state.access_events.deliver_due)
        retried = [p for p in sink.calls if p.get("name") == "message.received"]
        assert len(retried) == 2
        assert retried[1]["eventId"] == event["eventId"]
        with sqlite3.connect(app.state.settings.database_path) as db:
            assert db.execute(
                "SELECT read_at,replied_at,seen_count FROM coordination_message_recipients "
                "WHERE message_hash=? AND recipient_agent_id=?",
                (event["data"]["message_id"], a_id),
            ).fetchone() == (None, None, 0)
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM coordination_messages WHERE message_hash=?",
                    (event["data"]["message_id"],),
                ).fetchone()[0]
                == 1
            )


def test_only_access_advertises_events_and_unbound_oauth_client_cannot_subscribe(tmp_path):
    app = create_app(settings(tmp_path))
    sink = CallbackSink()
    app.state.access_events.sender = sink
    with TestClient(app, base_url=ISSUER) as client:
        _, token, bound_pair, _ = provision(client, app)
        for role in ("access", "executor", "coordinator"):
            discovered = call(client, role, "server/discover", token, bound_pair)
            assert "result" in discovered, discovered
            has_events = "events" in discovered["result"]["capabilities"]
            assert has_events == (role == "access")
            listed = call(client, role, "events/list", token, bound_pair)
            assert "result" in listed, listed
            names = {row["name"] for row in listed["result"]["events"]}
            assert names == (
                {"agent.attached", "agent.inactive", "session.ended", "message.received"}
                if role == "access"
                else set()
            )

        unrelated, _ = client.portal.call(
            app.state.oauth_store.register_client,
            ["https://other-connector.example.test/end"],
            "Unrelated client",
            "none",
        )
        now = int(time.time())
        foreign_token = jwt.encode(
            {
                "iss": ISSUER,
                "aud": ISSUER + "/mcp",
                "sub": unrelated,
                "iat": now,
                "exp": now + 3600,
                "scope": "terminal:read terminal:execute",
            },
            SIGNER,
            algorithm="HS256",
        )
        unauthorized = subscribe(client, foreign_token, bound_pair)
        assert "error" in unauthorized and unauthorized["error"]["code"] == -32001
        assert not sink.calls
        # The public name and a copied pair do not grant an unrelated client a mailbox.
        unbound = subscribe(
            client,
            token,
            {"openai/subject": "stolen-visible-name", "openai/session": "unbound-chat"},
        )
        assert "error" in unbound and unbound["error"]["code"] == -32001
        assert not sink.calls
