"""Secret-free ingress diagnostics for the Events JSON-RPC transport."""

from __future__ import annotations

import json
import logging
import re
import secrets
from datetime import UTC, datetime

_LOG = logging.getLogger("uvicorn.error")
_MCP_PATHS = (
    "/mcp",
    "/terminal-mcp/executor/v1/mcp",
    "/terminal-mcp/coordinator/v1/mcp",
    "/terminal-mcp/access/v1/mcp",
)
_METHODS = frozenset(
    {
        "server/discover",
        "events/list",
        "events/subscribe",
        "events/unsubscribe",
    }
)


def correlation_id() -> str:
    return "ev_" + secrets.token_hex(8)


def emit(method: str, stage: str, code: str, cid: str) -> None:
    """Log only protocol-level enums and a server-generated correlation ID."""
    values = {
        "utc": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "correlation_id": cid if re.fullmatch(r"ev_[a-f0-9]{16}", cid) else "unknown",
        "method": method if method in _METHODS or method == "events/worker" else "unknown",
        "stage": stage if re.fullmatch(r"[a-z_]{1,48}", stage) else "unknown",
        "code": code if re.fullmatch(r"[a-z_0-9]{1,64}", code) else "unclassified",
    }
    _LOG.info("tmcp_events_diag %s", json.dumps(values, separators=(",", ":")))


class MCPEventIngressProbe:
    """Capture Events ingress before OAuth middleware without logging request data.

    Only buffers small POST JSON-RPC requests. Forward the original byte stream to
    the existing ASGI stack, preserving signatures, authorization and tool traffic.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or not any(path == p or path.startswith(p + "/") for p in _MCP_PATHS)
        ):
            await self.app(scope, receive, send)
            return
        content_length = next(
            (v for k, v in scope.get("headers", []) if k.lower() == b"content-length"),
            b"",
        )
        try:
            if content_length and int(content_length) > 32768:
                await self.app(scope, receive, send)
                return
        except (ValueError, OverflowError):
            await self.app(scope, receive, send)
            return
        buffered = []
        length = 0
        complete = False
        while length <= 32768:
            msg = await receive()
            buffered.append(msg)
            if msg["type"] != "http.request":
                break
            length += len(msg.get("body", b""))
            if not msg.get("more_body", False):
                complete = True
                break
        method = None
        if complete and length <= 32768:
            try:
                parsed = json.loads(b"".join(m.get("body", b"") for m in buffered))
                if isinstance(parsed, dict) and parsed.get("method") in _METHODS:
                    method = parsed["method"]
            except (UnicodeError, ValueError):
                pass

        next_pos = 0

        async def replay_receive():
            nonlocal next_pos
            if next_pos < len(buffered):
                result = buffered[next_pos]
                next_pos += 1
                return result
            return await receive()

        if method is None:
            await self.app(scope, replay_receive, send)
            return
        cid = correlation_id()
        scope["mcp_events_cid"] = cid
        emit(method, "ingress", "received", cid)

        async def response_send(message):
            if message["type"] == "http.response.start":
                emit(method, "http_response", f"http_{int(message['status'])}", cid)
            await send(message)

        try:
            await self.app(scope, replay_receive, response_send)
        except Exception:
            emit(method, "http_response", "unhandled_exception", cid)
            raise
