from __future__ import annotations

import asyncio
import contextvars
import os
import time

current_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "terminal_mcp_trace_id", default=None
)
current_upstream_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "terminal_mcp_upstream_request_id", default=None
)


def uuid7() -> str:
    ts = int(time.time() * 1000) & ((1 << 48) - 1)
    rnd = int.from_bytes(os.urandom(10), "big")
    value = (
        (ts << 80)
        | (0x7 << 76)
        | ((rnd & 0xFFF) << 64)
        | (0x2 << 62)
        | ((rnd >> 12) & ((1 << 62) - 1))
    )
    h = f"{value:032x}"
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


class TraceMiddleware:
    def __init__(self, app, timeout_seconds: float = 22.0, events=None):
        self.app = app
        self.timeout_seconds = timeout_seconds
        self.events = events

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        from terminal_mcp.attempt_diagnostics import emit_attempt

        trace_id = uuid7()
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        upstream = next(
            (
                headers[n].decode("latin-1")
                for n in (b"traceparent", b"x-request-id", b"x-client-request-id")
                if n in headers
            ),
            None,
        )
        trace_token = current_trace_id.set(trace_id)
        upstream_token = current_upstream_request_id.set(upstream)
        route = scope.get("path", "").rstrip("/")
        roles = {
            f"/terminal-mcp/{role}/v1/mcp": role for role in ("access", "executor", "coordinator")
        }
        roles["/mcp"] = "legacy"
        role = roles.get(route)
        events = self.events if role else None
        started = time.monotonic()
        response_started = response_complete = disconnected = False
        response_status = body_received_ms = None
        received_bytes = sent_bytes = 0
        send_phase = None
        outcome, failure = "returned", None
        emit_attempt(events, "mcp_http_started", role=role)

        async def traced_receive():
            nonlocal received_bytes, body_received_ms, disconnected
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if not message.get("more_body", False):
                    body_received_ms = round((time.monotonic() - started) * 1000, 3)
            elif message["type"] == "http.disconnect":
                disconnected = True
            return message

        async def traced_send(message):
            nonlocal response_started, response_complete, response_status, sent_bytes, send_phase
            send_phase = message["type"]
            if message["type"] == "http.response.start":
                response_status = message["status"]
                values = list(message.get("headers", []))
                values.append((b"x-terminal-trace-id", trace_id.encode("ascii")))
                message = {**message, "headers": values}
            await send(message)
            # These are ASGI send acknowledgements, NOT proof of client receipt.
            if message["type"] == "http.response.start":
                response_started = True
            elif message["type"] == "http.response.body":
                sent_bytes += len(message.get("body", b""))
                if not message.get("more_body", False):
                    response_complete = True

        deadline = asyncio.timeout(self.timeout_seconds)
        try:
            async with deadline:
                await self.app(scope, traced_receive, traced_send)
        except TimeoutError:
            outcome = "timeout" if deadline.expired() else "exception"
            failure = "TimeoutError"
            raise
        except asyncio.CancelledError:
            outcome, failure = "cancelled", "CancelledError"
            raise
        except Exception as exc:
            outcome, failure = "exception", type(exc).__name__
            raise
        finally:
            emit_attempt(
                events,
                "mcp_http_finished",
                role=role,
                outcome=outcome,
                exception_class=failure,
                duration_ms=round((time.monotonic() - started) * 1000, 3),
                response_status=response_status,
                response_started=response_started,
                response_complete=response_complete,
                send_phase=send_phase,
                received_bytes=received_bytes,
                sent_bytes=sent_bytes,
                body_received_ms=body_received_ms,
                disconnect_observed=disconnected,
            )
            current_upstream_request_id.reset(upstream_token)
            current_trace_id.reset(trace_token)
