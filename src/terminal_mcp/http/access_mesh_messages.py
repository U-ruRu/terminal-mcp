"""Authenticated inbound HTTP adapter for Access Mesh message delivery."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from terminal_mcp.application.access_mesh_messages import MAX_WIRE_BYTES
from terminal_mcp.fleet.access_mesh import namespace_dependency


def build_access_mesh_message_router(messages, replication_auth):
    router = APIRouter(dependencies=[Depends(namespace_dependency(messages.mesh))])

    @router.post("/internal/fleet/access-mesh/messages/{kind}", include_in_schema=False)
    async def peer_message(
        kind: str,
        request: Request,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        peer = replication_auth.authenticate(x_terminal_mcp_peer, authorization)
        if peer is None or peer.instance_id not in {item.instance_id for item in messages.peers}:
            raise HTTPException(401, "invalid access mesh peer")
        raw = bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw) > MAX_WIRE_BYTES:
                raise HTTPException(413, "message payload exceeds byte budget")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(400, "invalid message payload") from exc
        if not isinstance(payload, dict):
            raise HTTPException(400, "invalid message payload")
        result = await messages.handle_peer(peer.instance_id, kind, payload)
        if (
            len(json.dumps(result, separators=(",", ":"), ensure_ascii=False).encode())
            > MAX_WIRE_BYTES
        ):
            raise HTTPException(413, "message response exceeds byte budget")
        return result

    @router.post("/internal/fleet/access-mesh/agents/locations", include_in_schema=False)
    async def peer_agent_locations(
        request: Request,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        peer = replication_auth.authenticate(x_terminal_mcp_peer, authorization)
        if peer is None or peer.instance_id not in {item.instance_id for item in messages.peers}:
            raise HTTPException(401, "invalid access mesh peer")
        raw = bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw) > MAX_WIRE_BYTES:
                raise HTTPException(413, "access mesh payload exceeds byte budget")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(400, "invalid access mesh payload") from exc
        if not isinstance(payload, dict):
            raise HTTPException(400, "invalid access mesh payload")
        result = await messages.handle_locations_peer(peer.instance_id, payload)
        if (
            len(json.dumps(result, separators=(",", ":"), ensure_ascii=False).encode())
            > MAX_WIRE_BYTES
        ):
            raise HTTPException(413, "access mesh response exceeds byte budget")
        return result

    return router
