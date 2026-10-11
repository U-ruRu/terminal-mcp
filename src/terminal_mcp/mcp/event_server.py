"""Authenticated MCP 2.0 Events adapter wrapped around the unchanged FastMCP app."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time

from starlette.responses import JSONResponse, Response

from terminal_mcp.core.orchestration import utc_text
from terminal_mcp.core.persistent_admission import current_admission_context
from terminal_mcp.mcp.event_diagnostics import correlation_id, emit, emit_callback_probe
from terminal_mcp.mcp.event_store import (
    EVENT_NAMES,
    MCPEventStore,
    RecipientBindingChangedError,
    SubscriptionAuthorizationError,
    event_catalog,
)
from terminal_mcp.mcp.event_webhooks import (
    CallbackEndpointError,
    post_signed,
    signing_key,
    validate_callback_url,
    verify_callback,
)
from terminal_mcp.mcp.message_binding import provider_metadata_from_events


class MCPEventsAdapter:
    def __init__(
        self,
        wrapped,
        store: MCPEventStore,
        *,
        reconcile=None,
        sender=None,
        access_checker=None,
        message_resolver=None,
        endpoint_role="legacy",
        events_enabled=True,
    ):
        self.wrapped = wrapped
        self.store = store
        self.reconcile = reconcile
        self.sender = sender
        self.access_checker = access_checker
        self.message_resolver = message_resolver
        self.endpoint_role = endpoint_role
        self.events_enabled = events_enabled
        self._task = None
        self._stop = asyncio.Event()
        self._last_worker_error = 0.0

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
                # Log only the failure class, never callback or private payload.
                # Rate-limited to avoid flooding the service journal.
                if time.monotonic() - self._last_worker_error >= 60:
                    emit("events/worker", "poll", "internal_error", correlation_id())
                    self._last_worker_error = time.monotonic()
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
    def _id(principal, name, arguments, callback, *, binding=None):
        extra = (
            [binding.recipient_agent_id, binding.connection_key, binding.slot_id]
            if binding is not None
            else []
        )
        canonical = json.dumps(
            [principal, callback, name, arguments, *extra],
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

    async def handle(self, request, *, cid=None):
        method = request["method"]
        cid = cid or correlation_id()

        def reject(stage, reason, code, message, data=None):
            emit(method, stage, reason, cid)
            return self._error(request.get("id"), code, message, data)

        request_id = request.get("id")
        params = request.get("params", {})
        if method == "server/discover":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "resultType": "complete",
                    "supportedVersions": ["2026-07-28"],
                    "capabilities": (
                        {"tools": {}, "events": {}} if self.events_enabled else {"tools": {}}
                    ),
                },
            }
        if method == "events/list":
            if not self.events_enabled:
                return {"jsonrpc": "2.0", "id": request_id, "result": {"events": []}}
            if params not in ({}, None) and (
                not isinstance(params, dict) or params.get("cursor") not in (None, "")
            ):
                return reject("validation", "invalid_parameters", -32602, "Invalid parameters")
            return {"jsonrpc": "2.0", "id": request_id, "result": event_catalog()}
        if not self.events_enabled:
            return reject("authorization", "events_access_only", -32601, "Method not found")
        principal = self._identity()
        if principal is None:
            return reject(
                "authorization",
                "principal_missing",
                -32001,
                "Authenticated principal required",
            )
        emit(method, "authorization", "accepted", cid)
        if not isinstance(params, dict):
            return reject("validation", "invalid_parameters", -32602, "Invalid parameters")
        name = params.get("name")
        arguments = params.get("arguments")
        delivery = params.get("delivery")
        number = arguments.get("session_number") if isinstance(arguments, dict) else None
        valid_arguments = arguments == {} or (
            name == "message.received"
            and isinstance(arguments, dict)
            and set(arguments) == {"session_number"}
            and isinstance(number, str)
            and re.fullmatch(r"[0-9]{4}", number) is not None
        )
        if name not in EVENT_NAMES or not valid_arguments or not isinstance(delivery, dict):
            return reject(
                "validation", "invalid_subscription", -32602, "Invalid event subscription"
            )
        if delivery.get("mode") != "webhook":
            return reject(
                "validation", "delivery_mode_invalid", -32602, "Webhook delivery required"
            )
        callback = delivery.get("url")
        try:
            validate_callback_url(callback)
        except CallbackEndpointError:
            return reject(
                "validation", "callback_url_invalid", -32602, "Invalid HTTPS callback URL"
            )
        emit(method, "validation", "accepted", cid)
        # Unsubscribe is authorized by the authenticated subscription owner,
        # including after a message recipient's Access/attach binding expires.
        # This cannot mint or rebind a recipient: it only removes the owner's
        # existing tuple and associated queued deliveries.
        if method == "events/unsubscribe":
            if set(delivery) - {"mode", "url"}:
                return reject(
                    "validation", "unsubscribe_invalid", -32602, "Invalid unsubscribe delivery"
                )
            await self.store.unsubscribe_matching(principal, name, callback, arguments)
            emit(method, "persistence", "unsubscribed", cid)
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        binding = None
        if name == "message.received":
            admission = current_admission_context()
            provider_meta = provider_metadata_from_events(params)
            if number is None and provider_meta is None:
                return reject(
                    "authorization",
                    "identity_metadata_missing",
                    -32001,
                    "Bound recipient metadata required",
                )
            if self.message_resolver is None:
                return reject(
                    "authorization",
                    "recipient_resolver_unavailable",
                    -32001,
                    "Recipient binding unavailable",
                )
            try:
                if number is not None:
                    binding = await self.message_resolver.resolve_number(
                        admission, number, self.endpoint_role
                    )
                else:
                    binding = await self.message_resolver.resolve(
                        admission,
                        provider_meta,
                        self.endpoint_role,
                    )
            except Exception:
                return reject(
                    "authorization",
                    "recipient_lookup_error",
                    -32001,
                    "Recipient binding unavailable",
                )
            if binding is None:
                return reject(
                    "authorization",
                    "recipient_not_bound",
                    -32001,
                    "Recipient binding unavailable",
                )
            emit(method, "authorization", "recipient_bound", cid)
        sub_id = self._id(principal, name, arguments, callback, binding=binding)
        if method != "events/subscribe":
            return reject("validation", "method_unknown", -32601, "Method not found")
        secret = delivery.get("secret")
        try:
            signing_key(secret)
        except (TypeError, ValueError, AttributeError):
            return reject("validation", "signing_key_invalid", -32602, "Invalid signing secret")
        if params.get("cursor") is not None:
            return reject("validation", "replay_unsupported", -32602, "Replay cursor unsupported")
        ttl_ms = params.get("ttlMs")
        if ttl_ms is not None and (type(ttl_ms) is not int or ttl_ms < 1000):
            return reject("validation", "ttl_invalid", -32602, "Invalid ttlMs")
        emit(method, "callback", "challenge_started", cid)
        callback_verified_fresh = False
        try:
            if not await self.store.verified_recently(principal, callback):
                await verify_callback(
                    callback,
                    secret,
                    sub_id,
                    sender=self.sender,
                    observer=lambda details: emit_callback_probe(cid, details),
                )
                callback_verified_fresh = True
            else:
                emit(method, "callback", "verification_cache_hit", cid)
        except CallbackEndpointError as exc:
            return reject(
                "callback",
                exc.reason,
                -32015,
                "CallbackEndpointError",
                {"reason": exc.reason},
            )
        emit(method, "callback", "verified", cid)
        admission = current_admission_context()
        if (
            admission is None
            or admission.principal_id != principal
            or (binding is not None and "terminal:read" not in admission.scopes)
        ):
            return reject(
                "authorization",
                "subscription_authorization_expired",
                -32001,
                "Subscription authorization expired",
            )
        try:
            expires_at = await self.store.save_subscription(
                sub_id=sub_id,
                principal=principal,
                credential_id=admission.credential_id,
                auth_mode=admission.auth_mode,
                name=name,
                callback=callback,
                secret=secret,
                ttl_ms=ttl_ms,
                callback_verified_fresh=callback_verified_fresh,
                message_binding=binding,
                authorization_checker=self.access_checker,
                arguments=arguments,
            )
        except RecipientBindingChangedError:
            return reject(
                "authorization",
                "recipient_binding_changed",
                -32001,
                "Session number recipient changed; unsubscribe before selecting it again",
                {"reason": "recipient_binding_changed"},
            )
        except SubscriptionAuthorizationError:
            return reject(
                "authorization",
                "subscription_authorization_expired",
                -32001,
                "Subscription authorization expired",
            )
        emit(method, "persistence", "stored", cid)
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
                        "capabilities": (
                            {"tools": {}, "events": {}} if self.events_enabled else {"tools": {}}
                        ),
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
            cid = scope.get("mcp_events_cid") or correlation_id()
            emit(request["method"], "rpc", "received", cid)
            try:
                result = await self.handle(request, cid=cid)
            except Exception:
                emit(request["method"], "rpc", "internal_error", cid)
                raise
            emit(
                request["method"],
                "rpc",
                "ok" if "result" in result else "rpc_error",
                cid,
            )
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
