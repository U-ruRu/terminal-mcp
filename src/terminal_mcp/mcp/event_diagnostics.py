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


_CALLBACK_RESULTS = frozenset(
    {
        "verified",
        "http_rejected",
        "invalid_json",
        "challenge_missing",
        "challenge_not_string",
        "challenge_mismatch",
        "transport_error",
        "dns_failed",
        "unsafe_address",
        "invalid_url",
        "response_too_large",
        "payload_too_large",
        "unexpected_error",
    }
)


# Only recognized response categories and header-presence booleans are retained.
_RESPONSE_CONTENT_TYPES = frozenset(
    {"application/json", "application/problem+json", "text/html", "text/plain"}
)
_RESPONSE_ERROR_CODES = frozenset(
    {
        "invalid_signature",
        "signature_mismatch",
        "missing_signature",
        "invalid_timestamp",
        "timestamp_expired",
        "invalid_secret",
        "unauthorized",
        "forbidden",
        "invalid_challenge",
    }
)


def safe_callback_response_metadata(details: dict) -> dict:
    """Second allowlist at the logging boundary; never truncate arbitrary strings."""
    safe = {}
    for key, allowed in {
        "response_content_type": _RESPONSE_CONTENT_TYPES | {"other", "missing"},
        "response_body_kind": {"json", "html", "empty", "other"},
        "response_error_code": _RESPONSE_ERROR_CODES,
    }.items():
        value = details.get(key)
        if isinstance(value, str) and value in allowed:
            safe[key] = value
    for key in (
        "callback_explicit_default_port",
        "response_cf_challenge",
        "response_request_id_present",
        "response_cf_ray_present",
    ):
        if type(details.get(key)) is bool:
            safe[key] = details[key]
    return safe


def classify_callback_response(body: bytes, getheader=None) -> dict:
    """Classify bounded bytes locally, emitting neither body text nor arbitrary headers."""
    if not isinstance(body, bytes) or len(body) > 65536:
        return {}
    details = {"response_body_kind": "empty" if not body else "other"}
    if body:
        try:
            parsed = json.loads(body)
        except (ValueError, TypeError, RecursionError):
            if body.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html")):
                details["response_body_kind"] = "html"
        else:
            details["response_body_kind"] = "json"
            if isinstance(parsed, dict):
                error = parsed.get("error")
                code = error.get("code") if isinstance(error, dict) else parsed.get("code")
                if isinstance(code, str) and code in _RESPONSE_ERROR_CODES:
                    details["response_error_code"] = code
    if getheader is not None:
        content_type = getheader("content-type")
        media_type = content_type.split(";", 1)[0].strip().lower() if content_type else "missing"
        details["response_content_type"] = (
            media_type if media_type in _RESPONSE_CONTENT_TYPES | {"missing"} else "other"
        )
        details["response_cf_challenge"] = getheader("cf-mitigated") == "challenge"
        details["response_request_id_present"] = bool(getheader("x-request-id"))
        details["response_cf_ray_present"] = bool(getheader("cf-ray"))
    return safe_callback_response_metadata(details)


def emit_callback_probe(cid: str, details: dict) -> None:
    """Emit ONLY bounded, non-identifying callback verification metadata.

    Never persist the destination URL, challenge, response body, arbitrary headers or key.
    Only allowlisted response categories and header-presence booleans are retained.
    These fields correlate server diagnosis with the pre-existing Events CID.
    """
    status = details.get("http_status")
    elapsed = details.get("elapsed_ms")
    body_size = details.get("body_size")
    code = details.get("result")
    values = {
        "correlation_id": cid if re.fullmatch(r"ev_[a-f0-9]{16}", cid) else "unknown",
        "result": code if code in _CALLBACK_RESULTS else "unexpected_error",
        "http_status": status if type(status) is int and 100 <= status <= 599 else None,
        "elapsed_ms": elapsed if type(elapsed) is int and 0 <= elapsed <= 120000 else None,
        "body_size": body_size if type(body_size) is int and 0 <= body_size <= 65537 else None,
        "json_valid": details.get("json_valid")
        if type(details.get("json_valid")) is bool
        else None,
        "challenge_present": details.get("challenge_present")
        if type(details.get("challenge_present")) is bool
        else None,
        "challenge_is_string": details.get("challenge_is_string")
        if type(details.get("challenge_is_string")) is bool
        else None,
        "challenge_matches": details.get("challenge_matches")
        if type(details.get("challenge_matches")) is bool
        else None,
    }
    values.update(safe_callback_response_metadata(details))
    _LOG.info("tmcp_events_callback %s", json.dumps(values, separators=(",", ":")))


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
