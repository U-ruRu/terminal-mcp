from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query, Request, WebSocket
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketDisconnect

from terminal_mcp.fleet.protocol import FLEET_PROTOCOL_MAJOR
from terminal_mcp.http.browser_security import canonical_origin
from terminal_mcp.http.console_events import _oauth_device


class FleetActivityRequest(BaseModel):
    since: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=1000)


class FleetQueryRequest(BaseModel):
    source_node_ids: list[str] | None = None
    cursor: str | None = None
    limit: int = Field(default=100, ge=1, le=100)
    q: str | None = Field(default=None, max_length=200)
    filters: dict[str, str | int | bool] = Field(default_factory=dict)
    as_of: str | None = None
    through_seq: int | None = Field(default=None, ge=0)
    include_count: bool = False
    include_facets: bool = False


class FleetNamespaceRequest(BaseModel):
    source_node_ids: list[str] | None = None
    cursor: str | None = None
    limit: int = Field(default=100, ge=1, le=100)
    q: str | None = Field(default=None, max_length=200)


def build_console_fleet_router(
    settings,
    auth,
    pairing_store,
    projection,
    ticket_store,
    projection_service=None,
) -> APIRouter:
    router = APIRouter()
    allowed_origins = frozenset(settings.browser_allowed_origins())

    def origin_allowed(value: str) -> bool:
        try:
            return canonical_origin(value) in allowed_origins
        except ValueError:
            return False

    async def authorize(request: Request):
        try:
            return await _oauth_device(request, auth, pairing_store)
        except Exception as exc:
            return JSONResponse(
                {"error": "unauthorized", "detail": str(exc)},
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )

    @router.get("/console/fleet/v1/probe", include_in_schema=False)
    async def probe(request: Request):
        denied = await authorize(request)
        if isinstance(denied, JSONResponse):
            return denied
        meta = await projection.meta()
        snapshot = await projection.snapshot()
        return JSONResponse(
            {
                "ok": True,
                "protocol_major": FLEET_PROTOCOL_MAJOR,
                "fleet_id": meta["fleet_id"],
                "node_id": meta["node_id"],
                "projection_epoch": meta["projection_epoch"],
                "projection_seq": meta["projection_seq"],
                "role": meta["role"],
                "owner_node_id": meta["owner_node_id"],
                "capabilities": (
                    projection_service.capabilities if projection_service else []
                ),
                "sources": snapshot["sources"],
                "scope_statuses": snapshot.get("scope_statuses") or [],
            },
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/console/fleet/v1/snapshot", include_in_schema=False)
    async def snapshot(request: Request):
        denied = await authorize(request)
        if isinstance(denied, JSONResponse):
            return denied
        return JSONResponse(
            await projection.snapshot(),
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/console/fleet/v1/activity", include_in_schema=False)
    async def activity(request: Request, body: FleetActivityRequest):
        denied = await authorize(request)
        if isinstance(denied, JSONResponse):
            return denied
        return JSONResponse(
            await projection.events(since=body.since, limit=body.limit),
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/console/fleet/v1/query/{resource}", include_in_schema=False)
    async def query_plane(request: Request, resource: str, body: FleetQueryRequest):
        denied = await authorize(request)
        if isinstance(denied, JSONResponse):
            return denied
        if projection_service is None:
            return JSONResponse(
                {"error": "query_v2_unavailable"},
                status_code=404,
                headers={"Cache-Control": "no-store"},
            )
        result = await projection_service.query(
            resource,
            source_node_ids=body.source_node_ids,
            cursor=body.cursor,
            limit=body.limit,
            q=body.q,
            filters=body.filters,
            as_of=body.as_of,
            through_seq=body.through_seq,
            include_count=body.include_count,
            include_facets=body.include_facets,
        )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.get("/console/fleet/v1/detail/{resource}", include_in_schema=False)
    async def query_detail(
        request: Request,
        resource: str,
        entity_id: str = Query(min_length=1, max_length=260),
        source_node_id: str | None = Query(default=None, max_length=128),
    ):
        denied = await authorize(request)
        if isinstance(denied, JSONResponse):
            return denied
        if projection_service is None:
            return JSONResponse(
                {"error": "query_v2_unavailable"},
                status_code=404,
                headers={"Cache-Control": "no-store"},
            )
        result = await projection_service.detail(
            resource,
            entity_id,
            source_node_ids=[source_node_id] if source_node_id else None,
        )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.post("/console/fleet/v1/namespaces", include_in_schema=False)
    async def query_namespaces(request: Request, body: FleetNamespaceRequest):
        denied = await authorize(request)
        if isinstance(denied, JSONResponse):
            return denied
        if projection_service is None:
            return JSONResponse(
                {"error": "query_v2_unavailable"},
                status_code=404,
                headers={"Cache-Control": "no-store"},
            )
        result = await projection_service.namespaces(
            source_node_ids=body.source_node_ids,
            cursor=body.cursor,
            limit=body.limit,
            q=body.q,
        )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.get("/console/fleet/v1/task-graph", include_in_schema=False)
    async def query_task_graph(
        request: Request,
        namespace: str = Query(min_length=1, max_length=120),
        task_id: str = Query(min_length=1, max_length=120),
        depth: int = Query(default=2, ge=0, le=8),
        source_node_id: str | None = Query(default=None, max_length=128),
    ):
        denied = await authorize(request)
        if isinstance(denied, JSONResponse):
            return denied
        if projection_service is None:
            return JSONResponse(
                {"error": "query_v2_unavailable"},
                status_code=404,
                headers={"Cache-Control": "no-store"},
            )
        result = await projection_service.task_graph(
            namespace=namespace,
            task_id=task_id,
            depth=depth,
            source_node_ids=[source_node_id] if source_node_id else None,
        )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.post("/console/fleet/v1/ws-ticket", include_in_schema=False)
    async def issue_ticket(request: Request):
        request_origin = request.headers.get("origin")
        if request_origin and not origin_allowed(request_origin):
            return JSONResponse(
                {"error": "origin_not_allowed"},
                status_code=403,
                headers={"Cache-Control": "no-store"},
            )
        denied = await authorize(request)
        if isinstance(denied, JSONResponse):
            return denied
        client_id, device_id = denied
        ticket, expires_in = await ticket_store.issue(client_id, device_id)
        return JSONResponse(
            {"ticket": ticket, "expires_in": expires_in},
            headers={"Cache-Control": "no-store"},
        )

    @router.websocket("/console/fleet/v1/events")
    async def events(websocket: WebSocket):
        if not origin_allowed(websocket.headers.get("origin", "")):
            await websocket.close(code=4403, reason="origin_not_allowed")
            return
        record = await ticket_store.consume(websocket.query_params.get("ticket", ""))
        if record is None:
            await websocket.close(code=4401, reason="invalid_ticket")
            return
        try:
            cursor = int(websocket.query_params.get("since", "0"))
            requested_epoch = int(websocket.query_params.get("projection_epoch", "0"))
            if cursor < 0 or requested_epoch < 0:
                raise ValueError
        except ValueError:
            await websocket.close(code=4400, reason="invalid_cursor")
            return

        meta = await projection.meta()
        if requested_epoch and requested_epoch != meta["projection_epoch"]:
            await websocket.accept()
            await websocket.send_json(
                {
                    "type": "reset_required",
                    "projection_epoch": meta["projection_epoch"],
                    "projection_seq": meta["projection_seq"],
                }
            )
            await websocket.close(code=4409, reason="projection_epoch_changed")
            return

        await websocket.accept()
        try:
            while True:
                page = await projection.events(
                    since=cursor,
                    limit=settings.console_ws_batch_size,
                )
                if page.get("reset_required"):
                    await websocket.send_json({"type": "reset_required", **page})
                    await websocket.close(code=4409, reason="projection_cursor_expired")
                    return
                if page["events"]:
                    await websocket.send_json({"type": "events", **page})
                    cursor = page["events"][-1]["projection_seq"]
                await asyncio.sleep(settings.console_ws_poll_sec)
        except (WebSocketDisconnect, asyncio.CancelledError):
            return

    return router
