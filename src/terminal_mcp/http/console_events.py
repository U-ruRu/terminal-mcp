from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import JSONResponse
from starlette.websockets import WebSocketDisconnect


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


def build_console_events_router(settings, auth, pairing_store, event_store, ticket_store):
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
