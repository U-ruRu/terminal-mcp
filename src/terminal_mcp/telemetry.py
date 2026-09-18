from __future__ import annotations

import asyncio
import contextvars
import time

current_transport: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "terminal_mcp_transport", default=None
)
current_tool: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "terminal_mcp_tool", default=None
)


async def observed(service, transport: str, tool: str, operation):
    transport_token = current_transport.set(transport)
    tool_token = current_tool.set(tool)
    started = time.monotonic()
    metrics = getattr(service, "metrics", None)
    events = getattr(service, "events", None)
    labels = (("transport", transport), ("tool", tool))
    if metrics:
        metrics.inc("terminal_mcp_inflight_requests", labels)
    try:
        result = await operation
        outcome = "success" if result.get("ok", False) else "error"
        return result
    except asyncio.CancelledError:
        outcome = "cancelled"
        if metrics:
            metrics.inc("terminal_mcp_upstream_cancellations_total", labels)
            metrics.inc("terminal_mcp_client_disconnects_total", labels)
        raise
    except Exception:
        outcome = "error"
        raise
    finally:
        duration = time.monotonic() - started
        outcome = locals().get("outcome", "error")
        if metrics:
            metrics.inc(
                "terminal_mcp_requests_total",
                labels + (("outcome", outcome),),
            )
            metrics.observe("terminal_mcp_request_duration_seconds", duration, labels)
            metrics.inc("terminal_mcp_response_completed_total", labels)
            metrics.inc("terminal_mcp_inflight_requests", labels, -1)
        if events:
            events.emit(
                "tool_completed",
                transport=transport,
                tool=tool,
                outcome=outcome,
                duration_ms=round(duration * 1000),
            )
        current_tool.reset(tool_token)
        current_transport.reset(transport_token)
