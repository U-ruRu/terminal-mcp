import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from terminal_mcp.http.console_events import WebSocketTicketStore
from terminal_mcp.http.console_fleet import build_console_fleet_router


class Settings:
    console_ws_batch_size = 100
    console_ws_poll_sec = 0.01

    def browser_allowed_origins(self):
        return ("https://console.example",)


class Auth:
    async def verify_access(self, token, scopes):
        if token != "good":
            raise PermissionError("invalid_token")
        assert scopes == ["terminal:read"]
        return {"sub": "client-1"}


class Pairing:
    async def active_device_for_client(self, client_id):
        if client_id == "client-1":
            return {"device_id": "device-1"}
        return None


class Projection:
    async def meta(self):
        return {
            "fleet_id": "fleet-a",
            "node_id": "projection-a",
            "owner_node_id": "projection-a",
            "role": "owner",
            "projection_epoch": 3,
            "projection_seq": 8,
            "updated_at": "now",
        }

    async def snapshot(self):
        return {
            **(await self.meta()),
            "sources": [{"source_node_id": "node-a", "freshness": "fresh"}],
            "entities": [],
            "runtime_overlays": [],
        }

    async def events(self, *, since, limit):
        return {
            "projection_epoch": 3,
            "projection_seq": 8,
            "events": [],
            "since": since,
            "limit": limit,
        }


@pytest.mark.asyncio
async def test_console_fleet_http_surface_requires_paired_oauth():
    app = FastAPI()
    tickets = WebSocketTicketStore(30)
    app.include_router(
        build_console_fleet_router(
            Settings(),
            Auth(),
            Pairing(),
            Projection(),
            tickets,
        )
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.get("/console/fleet/v1/probe")
        assert denied.status_code == 401

        headers = {"authorization": "Bearer good"}
        probe = await client.get("/console/fleet/v1/probe", headers=headers)
        assert probe.status_code == 200
        assert probe.json()["projection_epoch"] == 3
        assert probe.json()["projection_seq"] == 8

        snapshot = await client.get("/console/fleet/v1/snapshot", headers=headers)
        assert snapshot.status_code == 200
        assert snapshot.json()["owner_node_id"] == "projection-a"

        activity = await client.post(
            "/console/fleet/v1/activity",
            headers=headers,
            json={"since": 7, "limit": 25},
        )
        assert activity.status_code == 200
        assert activity.json()["projection_epoch"] == 3
        assert activity.json()["projection_seq"] == 8

        ticket = await client.post(
            "/console/fleet/v1/ws-ticket",
            headers={**headers, "origin": "https://console.example"},
        )
        assert ticket.status_code == 200
        assert ticket.json()["ticket"]
