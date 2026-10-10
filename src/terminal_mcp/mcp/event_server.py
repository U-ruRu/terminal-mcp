"""Authenticated MCP 2.0 Events adapter wrapped around the unchanged FastMCP app."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time

from starlette.responses import JSONResponse, Response

from terminal_mcp.core.orchestration import utc_text
from terminal_mcp.core.persistent_admission import current_admission_context
from terminal_mcp.mcp.event_store import EVENT_NAMES, MCPEventStore, event_catalog
from terminal_mcp.mcp.event_webhooks import (
    CallbackEndpointError,
    post_signed,
    signing_key,
    validate_callback_url,
    verify_callback,
)


class MCPEventsAdapter:
    def __init__(
        self,
        wrapped,
        store: MCPEventStore,
        *,
        reconcile=None,
        sender=None,
        access_checker=None,
    ):
        self.wrapped = wrapped
        self.store = store
        self.reconcile = reconcile
        self.sender = sender
        self.access_checker = access_checker
        self._task = None
        self._stop = asyncio.Event()

    async def start(self):
        await self.store.initialize()
        self._stop.clear()
        self._task = asyncio.create_task(self._worker(), name="mcp-event-webhooks")

    async def stop(self):
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _worker(self):
        last_reconcile = time.monotonic()
        while not self._stop.is_set():
            try:
                if self.reconcile is not None and time.monotonic() - last_reconcile >= 30:
                    await self.reconcile()
                    last_reconcile = time.monotonic()
                await self.store.pump()
                await self.deliver_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed poll does not lose the durable cursor or pending deliveries.
                # Do not log callback URLs, secrets, or payloads.
                pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=5)
            except TimeoutError:
                pass

    async def deliver_due(self):
        for item in await self.store.due_deliveries():
            try:
                # Fail closed when access can no longer be verified.
                valid = self.access_checker is not None and await self.access_checker(item)
            except Exception:
                # Temporary checker failure is retryable; it must never send.
                await self.store.finish_attempt(
                    item["event_id"],
                    item["subscription_id"],
                    status=None,
                )
                continue
            if not valid:
                # Revoked credentials invalidate the durable subscription as well.
                await self.store.unsubscribe(
                    item["subscription_id"],
                    item["principal_id"],
                )
                continue
            payload = {
                "eventId": item["event_id"],
                "name": item["name"],
                "timestamp": item["occurred_at"],
                "data": json.loads(item["data_json"]),
                "cursor": None,
            }
            status = None
            try:
                status, _ = await post_signed(
                    item["callback_url"],
                    item["signing_secret"],
                    item["subscription_id"],
                    item["event_id"],
                    payload,
                    sender=self.sender,
                    previous_secret=(
                        item["previous_signing_secret"]
                        if item["rotation_until"] and item["rotation_until"] > utc_text()
                        else None
                    ),
                )
            except CallbackEndpointError as exc:
                if exc.reason == "payload_too_large":
                    status = 413
            except Exception:
                # Every transport failure consumes a bounded retry attempt.
                pass
            await self.store.finish_attempt(
                item["event_id"],
                item["subscription_id"],
                status=status,
            )

    @staticmethod
    def _identity():
        # AuthMiddleware has validated the bearer/OAuth token before this route.
        admission = current_admission_context()
        if (
            admission is None
            or admission.transport != "mcp"
            or not ({"terminal:read", "terminal:execute"} & admission.scopes)
        ):
            return None
        return admission.principal_id

    @staticmethod
    def _id(principal, name, arguments, callback):
        canonical = json.dumps(
            [principal, callback, name, arguments],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return "sub_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:40]

    @staticmethod
    def _error(request_id, code, message, data=None):
        result = {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
        if data is not None:
            result["error"]["data"] = data
        return result

    async def handle(self, request):
        method = request["method"]
        request_id = request.get("id")
        params = request.get("params", {})
        if method == "server/discover":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "resultType": "complete",
                    "supportedVersions": ["2026-07-28"],
                    "capabilities": {"tools": {}, "events": {}},
                },
            }
        if method == "events/list":
            if params not in ({}, None) and (
                not isinstance(params, dict) or params.get("cursor") not in (None, "")
            ):
                return self._error(request_id, -32602, "Invalid parameters")
            return {"jsonrpc": "2.0", "id": request_id, "result": event_catalog()}
        principal = self._identity()
        if principal is None:
            return self._error(request_id, -32001, "Authenticated principal required")
        if not isinstance(params, dict):
            return self._error(request_id, -32602, "Invalid parameters")
        name = params.get("name")
        arguments = params.get("arguments")
        delivery = params.get("delivery")
        if name not in EVENT_NAMES or arguments != {} or not isinstance(delivery, dict):
            return self._error(request_id, -32602, "Invalid event subscription")
        if delivery.get("mode") != "webhook":
            return self._error(request_id, -32602, "Webhook delivery required")
        callback = delivery.get("url")
        try:
            validate_callback_url(callback)
        except CallbackEndpointError:
            return self._error(request_id, -32602, "Invalid HTTPS callback URL")
        sub_id = self._id(principal, name, arguments, callback)
        if method == "events/unsubscribe":
            if set(delivery) - {"mode", "url"}:
                return self._error(request_id, -32602, "Invalid unsubscribe delivery")
            await self.store.unsubscribe(sub_id, principal)
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method != "events/subscribe":
            return self._error(request_id, -32601, "Method not found")
        secret = delivery.get("secret")
        try:
            signing_key(secret)
        except (TypeError, ValueError, AttributeError):
            return self._error(request_id, -32602, "Invalid signing secret")
        if params.get("cursor") is not None:
            return self._error(request_id, -32602, "Replay cursor unsupported")
        ttl_ms = params.get("ttlMs")
        if ttl_ms is not None and (type(ttl_ms) is not int or ttl_ms < 1000):
            return self._error(request_id, -32602, "Invalid ttlMs")
        try:
            if not await self.store.verified_recently(principal, callback):
                await verify_callback(
                    callback,
                    secret,
                    sub_id,
                    sender=self.sender,
                )
        except CallbackEndpointError as exc:
            return self._error(
                request_id,
                -32015,
                "CallbackEndpointError",
                {"reason": exc.reason},
            )
        admission = current_admission_context()
        expires_at = await self.store.save_subscription(
            sub_id=sub_id,
            principal=principal,
            credential_id=admission.credential_id,
            auth_mode=admission.auth_mode,
            name=name,
            callback=callback,
            secret=secret,
            ttl_ms=ttl_ms,
        )
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "id": sub_id,
                "refreshBefore": expires_at,
                "cursor": None,
                "truncated": False,
            },
        }

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"].upper() != "POST":
            await self.wrapped(scope, receive, send)
            return
        # Preserve every existing FastMCP initialize/tools request byte-for-byte.
        chunks = []
        total = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                await self.wrapped(scope, receive, send)
                return
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > 1024 * 1024:
                await Response(status_code=413)(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        try:
            request = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            request = None
        # MCP 2.0 Events is a protocol facade over the existing FastMCP 1.x
        # tool stack. Negotiate the new wire version only for clients that
        # explicitly request it; legacy clients retain the unchanged handshake.
        if (
            isinstance(request, dict)
            and request.get("jsonrpc") == "2.0"
            and request.get("method") == "initialize"
            and isinstance(request.get("params"), dict)
            and request["params"].get("protocolVersion") == "2026-07-28"
        ):
            await JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": request.get("id"),
                    "result": {
                        "protocolVersion": "2026-07-28",
                        "capabilities": {"tools": {}, "events": {}},
                        "serverInfo": {"name": "terminal-mcp", "version": "2.0"},
                    },
                }
            )(scope, receive, send)
            return

        if (
            isinstance(request, dict)
            and request.get("jsonrpc") == "2.0"
            and request.get("method")
            in (
                "server/discover",
                "events/list",
                "events/subscribe",
                "events/unsubscribe",
            )
        ):
            result = await self.handle(request)
            await JSONResponse(result)(scope, receive, send)
            return
        has_replayed = False

        async def replay_receive():
            nonlocal has_replayed
            if not has_replayed:
                has_replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        # Installed FastMCP SDK predates 2026-07-28. Its handler for the
        # unmodified tool methods still understands 2025-11-25. Translate
        # only this transport header before delegation, never auth headers.
        if any(
            key.lower() == b"mcp-protocol-version" and val == b"2026-07-28"
            for key, val in scope.get("headers", [])
        ):
            scope = dict(scope)
            scope["headers"] = [
                (key, b"2025-11-25")
                if key.lower() == b"mcp-protocol-version" and val == b"2026-07-28"
                else (key, val)
                for key, val in scope.get("headers", [])
            ]
        await self.wrapped(scope, replay_receive, send)
