"""Fleet HTTP source adapter authentication and query forwarding over fake ports."""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from terminal_mcp.http.fleet_v1 import build_fleet_v1_source_router


class _Peer:
    instance_id = "peer-a"


class _Replication:
    def authenticate(self, peer_id, authorization):
        if peer_id == "peer-a" and authorization == "Bearer token":
            return _Peer()
        return None


class _Source:
    async def manifest(self):
        return {"protocol_major": 1}

    async def events(self, **kwargs):
        return {"events": [], **kwargs}

    async def snapshot(self):
        return {"replace": True}


@pytest.mark.asyncio
async def test_source_http_surface_reuses_fleet_peer_authentication():
    app = FastAPI()
    app.include_router(build_fleet_v1_source_router(_Source(), _Replication()))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        unauthorized = await client.get("/internal/fleet/v1/source/manifest")
        assert unauthorized.status_code == 401
        headers = {
            "x-terminal-mcp-peer": "peer-a",
            "authorization": "Bearer token",
        }
        manifest = await client.get("/internal/fleet/v1/source/manifest", headers=headers)
        assert manifest.status_code == 200
        assert manifest.json()["protocol_major"] == 1


@pytest.mark.asyncio
async def test_source_http_events_forwards_query_arguments():
    app = FastAPI()
    app.include_router(build_fleet_v1_source_router(_Source(), _Replication()))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {
            "x-terminal-mcp-peer": "peer-a",
            "authorization": "Bearer token",
        }
        events = await client.get(
            "/internal/fleet/v1/source/events",
            headers=headers,
            params={"since": 4, "limit": 17, "source_stream_generation": "gen-a"},
        )
        assert events.status_code == 200
        assert events.json()["since"] == 4
        assert events.json()["limit"] == 17
        assert events.json()["source_stream_generation"] == "gen-a"
