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


def subscribe(client, token, evidence, number):
    return call(
        client,
        "access",
        "events/subscribe",
        token,
        evidence,
        name="message.received",
        arguments={"session_number": number},
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
        result = subscribe(
            client,
            token,
            evidence,
            app.state.access_mesh.store.numbers.number_for_slot(slot["issuer_id"], slot["slot_id"]),
        )
        assert "error" in result, result
        assert sink.calls == []  # strict authorization before outbound callback challenge
        with sqlite3.connect(app.state.settings.database_path) as db:
            assert db.execute("SELECT COUNT(*) FROM mcp_event_subscriptions").fetchone()[0] == 0


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
        result = subscribe(
            client,
            token,
            evidence,
            app.state.access_mesh.store.numbers.number_for_slot(slot["issuer_id"], slot["slot_id"]),
        )
        assert "error" in result, (
            "BLOCKER: private subscription saved after revocation during callback verification",
            result,
        )
        with sqlite3.connect(app.state.settings.database_path) as db:
            assert db.execute("SELECT COUNT(*) FROM mcp_event_message_bindings").fetchone()[0] == 0
