from __future__ import annotations

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
    def __init__(self, app, timeout_seconds: float = 22.0):
        self.app = app
        self.timeout_seconds = timeout_seconds

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
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

        async def traced_send(message):
            if message["type"] == "http.response.start":
                values = list(message.get("headers", []))
                values.append((b"x-terminal-trace-id", trace_id.encode("ascii")))
                message = {**message, "headers": values}
            await send(message)

        try:
            import asyncio

            async with asyncio.timeout(self.timeout_seconds):
                await self.app(scope, receive, traced_send)
        finally:
            current_upstream_request_id.reset(upstream_token)
            current_trace_id.reset(trace_token)
