import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from terminal_mcp.app import create_app
from terminal_mcp.config import Settings
from terminal_mcp.http.console_events import WebSocketTicketStore, build_console_events_router
from terminal_mcp.storage.access_mesh_messages import AccessMeshMessageStore
from terminal_mcp.storage.agents import AgentStore
from terminal_mcp.storage.events import EventJournalStore
from terminal_mcp.storage.sqlite import SqliteRepository

ORIGIN = "https://terminal.example"


def settings(tmp_path, **updates):
    base = Settings(
        database_path=tmp_path / "db.sqlite3",
        auth_database_path=tmp_path / "auth.sqlite3",
        output_cache_path=tmp_path / "output.sqlite3",
        runtime_config_path=tmp_path / "runtime.env",
        log_path=tmp_path / "terminal-mcp.log",
        metrics_port=0,
        access_mesh_enabled=True,
        fleet_instance_id="test-node",
        fleet_signing_private_key="AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
        fleet_peers_json="[]",
        access_mesh_proof_key="mesh-runtime-fixture-key-never-production",
        access_mesh_peers_json="[]",
        fleet_v1_source_enabled=False,
        fleet_v1_authority_enabled=False,
        fleet_v1_projection_enabled=False,
        fleet_v1_public_enabled=False,
        persistent_agents_enabled=True,
        legacy_agent_admission_enabled=True,
        cwd=tmp_path,
        public_base_url=ORIGIN,
        oauth_issuer=ORIGIN,
        oauth_audience=f"{ORIGIN}/mcp",
        oauth_signing_secret="test-secret-that-is-at-least-32-bytes",
        oauth_required_scopes="terminal:read",
        oauth_access_ttl_sec=900,
        oauth_refresh_ttl_sec=3600,
        mcp_auth_mode="oauth",
        actions_auth_mode="oauth",
        console_ws_heartbeat_sec=0.02,
        console_ws_auth_check_sec=0.01,
        console_ws_poll_sec=0.005,
        console_ws_batch_size=1,
    )
    return base.model_copy(update=updates)


def pair(client, app):
    secret = asyncio.run(app.state.pairing_store.create())
    response = client.post(
        "/pairing/exchange",
        json={
            "code": secret,
            "device_label": "Browser",
            "public_key": "k" * 64,
        },
    )
    assert response.status_code == 200
    return response.json()


def ticket(client, access_token, *, origin=ORIGIN):
    response = client.post(
        "/console/ws-ticket",
        headers={"Authorization": f"Bearer {access_token}", "Origin": origin},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    return response.json()["ticket"]


@pytest.fixture
def current_activity_app(tmp_path):
    path = tmp_path / "activity.sqlite3"
    journal = EventJournalStore(path)

    async def initialize():
        await SqliteRepository(path).initialize()
        await journal.initialize()

    asyncio.run(initialize())
    AccessMeshMessageStore(path, "node-a").accept(
        {
            "message_hash": "node-a:localmsg:feed",
            "origin_node_id": "node-a",
            "created_at": datetime.now(UTC).isoformat(),
            "sender_id": "la_sender",
            "sender_name": "Alpha-1",
            "target": "Bravo-1",
            "text": "Operational hello",
            "mode": "notify",
            "scope": "local",
            "require_reply": False,
            "alert": False,
            "namespace": None,
            "task_id": None,
            "reply_to": None,
        },
        [{"logical_agent_id": "la_recipient", "public_name": "Bravo-1"}],
    )
    auth = SimpleNamespace(verify_access=AsyncMock(return_value={"sub": "console-client"}))
    devices = SimpleNamespace(
        active_device_for_client=AsyncMock(return_value={"device_id": "console-device"})
    )
    app = FastAPI()
    app.include_router(
        build_console_events_router(
            SimpleNamespace(browser_allowed_origins=lambda: [ORIGIN]),
            auth,
            devices,
            SimpleNamespace(agent_store=AgentStore(path)),
            journal,
            WebSocketTicketStore(),
        )
    )
    return app


def test_activity_feed_projects_authorized_message(current_activity_app):
    with TestClient(current_activity_app) as client:
        response = client.post(
            "/console/activity",
            headers={"Authorization": "Bearer current-device-token", "Origin": ORIGIN},
            json={"since": 0, "limit": 1, "entity_types": ["message"]},
        )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    (event,) = response.json()["events"]
    assert event["event_type"] == "message.created"
    assert event["actor_id"] == "la_sender"
    assert event["message"]["message_hash"] == "node-a:localmsg:feed"
    assert event["message"]["text"] == "Operational hello"
    assert [item["agent_id"] for item in event["message"]["recipients"]] == ["la_recipient"]


async def test_websocket_ticket_is_consumed_once():
    store = WebSocketTicketStore(ttl_seconds=10)
    value, _ = await store.issue("client", "device")
    await store.consume(value)

    assert await store.consume(value) is None


def test_websocket_replays_ordered_events_in_bounded_batches(tmp_path):
    app = create_app(settings(tmp_path, console_ws_batch_size=1))
    with TestClient(app) as client:
        paired = pair(client, app)
        for index in range(3):
            asyncio.run(
                app.state.event_store.append(
                    "test.changed",
                    "test",
                    str(index),
                    payload={"index": index},
                )
            )
        ws_ticket = ticket(client, paired["access_token"])
        ws_url = f"/console/events?ticket={ws_ticket}&since=0"

        with client.websocket_connect(
            ws_url,
            headers={"Origin": ORIGIN},
        ) as websocket:
            messages = [websocket.receive_json() for _ in range(3)]

        assert [item["type"] for item in messages] == ["event", "event", "event"]
        assert [item["event"]["payload"]["index"] for item in messages] == [0, 1, 2]


def test_open_websocket_closes_after_device_revocation(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        paired = pair(client, app)
        cursor = asyncio.run(app.state.event_store.high_water_seq())
        ws_ticket = ticket(client, paired["access_token"])
        with client.websocket_connect(
            f"/console/events?ticket={ws_ticket}&since={cursor}",
            headers={"Origin": ORIGIN},
        ) as websocket:
            assert asyncio.run(app.state.pairing_store.revoke_device(paired["device_id"]))
            with pytest.raises(WebSocketDisconnect) as closed:
                while True:
                    frame = websocket.receive_json()
                    assert frame["type"] == "heartbeat"
            assert closed.value.code == 4401


def test_websocket_reports_resync_required_on_journal_gap(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        paired = pair(client, app)
        for index in range(3):
            asyncio.run(app.state.event_store.append("test.changed", "test", str(index)))
        import sqlite3

        with sqlite3.connect(app.state.settings.database_path) as db:
            db.execute(
                "DELETE FROM instance_events WHERE seq=(SELECT MIN(seq) FROM instance_events)"
            )
            db.commit()

        ws_ticket = ticket(client, paired["access_token"])
        with client.websocket_connect(
            f"/console/events?ticket={ws_ticket}&since=0",
            headers={"Origin": ORIGIN},
        ) as websocket:
            message = websocket.receive_json()
            assert message["type"] == "resync_required"
            assert message["reason"] == "journal_gap"
            with pytest.raises(WebSocketDisconnect) as closed:
                websocket.receive_json()
            assert closed.value.code == 4409
