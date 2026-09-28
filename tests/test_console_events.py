import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from terminal_mcp.app import create_app
from terminal_mcp.config import Settings
from terminal_mcp.http.console_events import WebSocketTicketStore

ORIGIN = "https://terminal.example"


def settings(tmp_path, **updates):
    base = Settings(
        database_path=tmp_path / "db.sqlite3",
        output_cache_path=tmp_path / "output.sqlite3",
        runtime_config_path=tmp_path / "runtime.env",
        log_path=tmp_path / "terminal-mcp.log",
        metrics_port=0,
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
            "secret": secret,
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


def test_ticket_requires_paired_oauth_device_and_same_origin(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        paired = pair(client, app)
        missing = client.post("/console/ws-ticket", headers={"Origin": ORIGIN})
        wrong_origin = client.post(
            "/console/ws-ticket",
            headers={
                "Origin": "https://evil.example",
                "Authorization": f"Bearer {paired['access_token']}",
            },
        )
        issued = client.post(
            "/console/ws-ticket",
            headers={
                "Origin": ORIGIN,
                "Authorization": f"Bearer {paired['access_token']}",
            },
        )

    assert missing.status_code == 401
    assert wrong_origin.status_code == 403
    assert wrong_origin.json() == {"error": "origin_not_allowed"}
    assert issued.status_code == 200
    assert issued.json()["expires_in"] == 30
    assert issued.json()["ticket"]


@pytest.mark.asyncio
async def test_ticket_store_is_single_use_and_expiring(monkeypatch):
    now = 100.0
    monkeypatch.setattr("terminal_mcp.http.console_events.time.time", lambda: now)
    store = WebSocketTicketStore(ttl_seconds=10)

    first, _ = await store.issue("client", "device")
    record = await store.consume(first)
    assert record is not None
    assert record.client_id == "client"
    assert await store.consume(first) is None

    second, _ = await store.issue("client", "device")
    now = 111.0
    assert await store.consume(second) is None


def test_websocket_replays_in_bounded_batches_and_ticket_cannot_replay(tmp_path):
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
        assert paired["access_token"] not in ws_url

        with client.websocket_connect(
            ws_url,
            headers={"Origin": ORIGIN},
        ) as websocket:
            messages = [websocket.receive_json() for _ in range(3)]

        assert [item["type"] for item in messages] == ["event", "event", "event"]
        assert [item["event"]["payload"]["index"] for item in messages] == [0, 1, 2]

        with pytest.raises(WebSocketDisconnect) as replay:
            with client.websocket_connect(
                f"/console/events?ticket={ws_ticket}&since=0",
                headers={"Origin": ORIGIN},
            ):
                pass
        assert replay.value.code == 4401


def test_websocket_rejects_wrong_origin_and_revoked_device(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        paired = pair(client, app)
        wrong_origin_ticket = ticket(client, paired["access_token"])
        with pytest.raises(WebSocketDisconnect) as wrong_origin:
            with client.websocket_connect(
                f"/console/events?ticket={wrong_origin_ticket}&since=0",
                headers={"Origin": "https://evil.example"},
            ):
                pass
        assert wrong_origin.value.code == 4403

        revoked_ticket = ticket(client, paired["access_token"])
        assert asyncio.run(app.state.pairing_store.revoke_device(paired["device_id"]))
        with pytest.raises(WebSocketDisconnect) as revoked:
            with client.websocket_connect(
                f"/console/events?ticket={revoked_ticket}&since=0",
                headers={"Origin": ORIGIN},
            ):
                pass
        assert revoked.value.code == 4401




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
                websocket.receive_json()
            assert closed.value.code == 4401


def test_websocket_reports_resync_required_on_journal_gap(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        paired = pair(client, app)
        for index in range(3):
            asyncio.run(
                app.state.event_store.append("test.changed", "test", str(index))
            )
        import sqlite3

        with sqlite3.connect(app.state.settings.database_path) as db:
            db.execute(
                "DELETE FROM instance_events "
                "WHERE seq=(SELECT MIN(seq) FROM instance_events)"
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


def test_websocket_heartbeat_carries_cursor_and_high_water(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        paired = pair(client, app)
        high_water = asyncio.run(app.state.event_store.high_water_seq())
        ws_ticket = ticket(client, paired["access_token"])
        with client.websocket_connect(
            f"/console/events?ticket={ws_ticket}&since={high_water}",
            headers={"Origin": ORIGIN},
        ) as websocket:
            heartbeat = websocket.receive_json()

    assert heartbeat["type"] == "heartbeat"
    assert heartbeat["cursor"] == high_water
    assert heartbeat["high_water_seq"] == high_water
