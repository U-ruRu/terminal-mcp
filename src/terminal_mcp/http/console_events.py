from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketDisconnect

from terminal_mcp.core.orchestration import public_agent_name


class ConsoleTaskDetailRequest(BaseModel):
    namespace: str = Field(min_length=1, max_length=120)
    task_id: str = Field(min_length=1, max_length=120)


class ConsoleActivityRequest(BaseModel):
    since: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=200)
    event_types: list[str] = Field(default_factory=list, max_length=50)
    entity_types: list[str] = Field(default_factory=list, max_length=20)


@dataclass(frozen=True)
class WebSocketTicket:
    client_id: str
    device_id: str
    expires_at: float


class WebSocketTicketStore:
    def __init__(self, ttl_seconds: int = 30):
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.ttl_seconds = int(ttl_seconds)
        self._tickets: dict[str, WebSocketTicket] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _digest(ticket: str) -> str:
        return hashlib.sha256(ticket.encode()).hexdigest()

    async def issue(self, client_id: str, device_id: str) -> tuple[str, int]:
        ticket = secrets.token_urlsafe(32)
        now = time.time()
        async with self._lock:
            self._purge(now)
            self._tickets[self._digest(ticket)] = WebSocketTicket(
                client_id=client_id,
                device_id=device_id,
                expires_at=now + self.ttl_seconds,
            )
        return ticket, self.ttl_seconds

    async def consume(self, ticket: str) -> WebSocketTicket | None:
        if not ticket:
            return None
        now = time.time()
        async with self._lock:
            self._purge(now)
            record = self._tickets.pop(self._digest(ticket), None)
        if record is None or record.expires_at <= now:
            return None
        return record

    def _purge(self, now: float) -> None:
        expired = [key for key, item in self._tickets.items() if item.expires_at <= now]
        for key in expired:
            self._tickets.pop(key, None)


def _origin(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{parsed.scheme.lower()}://{host}{suffix}"


async def _oauth_device(request: Request, auth, pairing_store):
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not token:
        raise PermissionError("missing_token")
    claims = await auth.verify_access(token, ["terminal:read"])
    client_id = str(claims.get("sub", ""))
    device = await pairing_store.active_device_for_client(client_id)
    if device is None:
        raise PermissionError("device_required")
    return client_id, device["device_id"]


async def _project_activity_event(service, event: dict) -> dict:
    projected = {
        "seq": event["seq"],
        "event_type": event["event_type"],
        "entity_type": event["entity_type"],
        "entity_id": event["entity_id"],
        "payload": event.get("payload") or {},
        "created_at": event["created_at"],
    }
    actor_id = event.get("actor_id")
    if actor_id:
        projected["actor_name"] = public_agent_name(actor_id)
    if event["entity_type"] == "message" and service.agent_store:
        message = await service.agent_store.message_record(event["entity_id"])
        if message is not None:
            receipts = await service.agent_store.message_receipts(event["entity_id"])
            projected["message"] = {
                "message_hash": message["message_hash"],
                "sender_name": public_agent_name(message["sender_agent_id"]),
                "target": message["target_name"] or "broadcast",
                "text": message["text"],
                "require_reply": bool(message["require_reply"]),
                "alert": bool(message["alert"]),
                "task_namespace": message["task_namespace"],
                "task_id": message["task_id"],
                "recipients": [
                    {
                        "name": public_agent_name(item["agent_id"]),
                        "seen": bool(item["seen"]),
                        "read": bool(item["read"]),
                        "replied": bool(item["replied"]),
                    }
                    for item in receipts
                ],
            }
    return projected


async def _activity_page(service, event_store, body: ConsoleActivityRequest) -> dict:
    event_types = {item.strip() for item in body.event_types if item.strip()}
    entity_types = {item.strip() for item in body.entity_types if item.strip()}
    cursor = body.since
    events = []
    first_page = None
    high_water = body.since
    scanned = 0
    max_scan = 5000
    while len(events) < body.limit and scanned < max_scan:
        scan_limit = min(1000, max(100, body.limit * 4), max_scan - scanned)
        page = await event_store.read(since=cursor, limit=scan_limit)
        if first_page is None:
            first_page = page
        high_water = page["high_water_seq"]
        if not page["events"]:
            break
        for event in page["events"]:
            cursor = event["seq"]
            scanned += 1
            if event_types and event["event_type"] not in event_types:
                continue
            if entity_types and event["entity_type"] not in entity_types:
                continue
            events.append(await _project_activity_event(service, event))
            if len(events) >= body.limit:
                break
        if len(events) >= body.limit or cursor >= high_water:
            break
    first_page = first_page or await event_store.read(since=body.since, limit=1)
    return {
        "ok": True,
        "events": events,
        "since": body.since,
        "next_cursor": cursor,
        "oldest_seq": first_page["oldest_seq"],
        "high_water_seq": high_water,
        "gap": first_page["gap"],
        "gap_from_seq": first_page["gap_from_seq"],
        "gap_to_seq": first_page["gap_to_seq"],
    }


def build_console_events_router(settings, auth, pairing_store, service, event_store, ticket_store):
    router = APIRouter()
    expected_origin = _origin(settings.public_base_url)

    @router.post("/console/ws-ticket", include_in_schema=False)
    async def issue_ticket(request: Request):
        request_origin = request.headers.get("origin")
        if request_origin and _origin(request_origin) != expected_origin:
            return JSONResponse(
                {"error": "origin_not_allowed"},
                status_code=403,
                headers={"Cache-Control": "no-store"},
            )
        try:
            client_id, device_id = await _oauth_device(request, auth, pairing_store)
        except Exception as exc:
            return JSONResponse(
                {"error": "unauthorized", "detail": str(exc)},
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )
        ticket, expires_in = await ticket_store.issue(client_id, device_id)
        return JSONResponse(
            {"ticket": ticket, "expires_in": expires_in},
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/console/task-detail", include_in_schema=False)
    async def task_detail(request: Request, body: ConsoleTaskDetailRequest):
        try:
            await _oauth_device(request, auth, pairing_store)
        except Exception as exc:
            return JSONResponse(
                {"error": "unauthorized", "detail": str(exc)},
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )
        result = await service.tasks(
            namespace=body.namespace,
            task_id=body.task_id,
            show_details=True,
            show_done=True,
            show_archived=True,
            limit=1,
            cursor=0,
        )
        task = result.get("task") if result.get("ok") else None
        if task is None:
            return JSONResponse(
                {"error": "task_not_found"},
                status_code=404,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(
            {"ok": True, "task": task},
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/console/activity", include_in_schema=False)
    async def activity(request: Request, body: ConsoleActivityRequest):
        try:
            await _oauth_device(request, auth, pairing_store)
        except Exception as exc:
            return JSONResponse(
                {"error": "unauthorized", "detail": str(exc)},
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )
        result = await _activity_page(service, event_store, body)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.websocket("/console/events")
    async def console_events(websocket: WebSocket):
        if _origin(websocket.headers.get("origin", "")) != expected_origin:
            await websocket.close(code=4403, reason="origin_not_allowed")
            return

        try:
            since = int(websocket.query_params.get("since", "0"))
            if since < 0:
                raise ValueError
        except ValueError:
            await websocket.close(code=4400, reason="invalid_cursor")
            return

        record = await ticket_store.consume(websocket.query_params.get("ticket", ""))
        if record is None:
            await websocket.close(code=4401, reason="invalid_ticket")
            return
        if not await pairing_store.device_active(record.device_id, record.client_id):
            await websocket.close(code=4401, reason="revoked_device")
            return

        await websocket.accept()
        cursor = since
        heartbeat = float(settings.console_ws_heartbeat_sec)
        auth_check = float(settings.console_ws_auth_check_sec)
        poll = float(settings.console_ws_poll_sec)
        batch = int(settings.console_ws_batch_size)
        last_send = time.monotonic()
        last_auth_check = last_send

        try:
            while True:
                now = time.monotonic()
                if now - last_auth_check >= auth_check:
                    if not await pairing_store.device_active(record.device_id, record.client_id):
                        await websocket.close(code=4401, reason="revoked_device")
                        return
                    last_auth_check = now

                page = await event_store.read(since=cursor, limit=batch)
                if page["gap"]:
                    await websocket.send_json(
                        {
                            "type": "resync_required",
                            "reason": "journal_gap",
                            "cursor": cursor,
                            "oldest_seq": page["oldest_seq"],
                            "high_water_seq": page["high_water_seq"],
                        }
                    )
                    await websocket.close(code=4409, reason="resync_required")
                    return

                if page["events"]:
                    for event in page["events"]:
                        await websocket.send_json({"type": "event", "event": event})
                        cursor = event["seq"]
                    last_send = time.monotonic()
                    continue

                now = time.monotonic()
                if now - last_send >= heartbeat:
                    await websocket.send_json(
                        {
                            "type": "heartbeat",
                            "cursor": cursor,
                            "high_water_seq": page["high_water_seq"],
                        }
                    )
                    last_send = now
                await asyncio.sleep(poll)
        except WebSocketDisconnect:
            return

    return router
