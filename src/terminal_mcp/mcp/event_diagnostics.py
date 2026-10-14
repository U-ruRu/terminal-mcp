"""Opt-in, secret-free Events transport diagnostics and minimal operational failures."""

from __future__ import annotations

import json
import logging
import re
import secrets
from datetime import UTC, datetime
from html.parser import HTMLParser

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


def diagnostics_enabled(setting) -> bool:
    """Read the existing runtime flag, failing closed if its provider is unavailable."""
    try:
        return (setting() if callable(setting) else setting) is True
    except Exception:
        return False


def emit(method: str, stage: str, code: str, cid: str, *, enabled=False) -> None:
    """Log only protocol-level enums and a server-generated correlation ID, when opted in."""
    if not diagnostics_enabled(enabled):
        return
    values = {
        "utc": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "correlation_id": cid if re.fullmatch(r"ev_[a-f0-9]{16}", cid) else "unknown",
        "method": method if method in _METHODS or method == "events/worker" else "unknown",
        "stage": stage if re.fullmatch(r"[a-z_]{1,48}", stage) else "unknown",
        "code": code if re.fullmatch(r"[a-z_0-9]{1,64}", code) else "unclassified",
    }
    try:
        _LOG.info("tmcp_events_diag %s", json.dumps(values, separators=(",", ":")))
    except Exception:
        # Diagnostic sinks must never alter protocol or delivery outcomes.
        pass


# Only metadata SHAPE is diagnostic: never raw values, signatures, arbitrary
# header values, opaque conversation IDs, callback addresses or credentials.
_META_KNOWN_KEYS = (
    "openai/subject",
    "openai/session",
    "openai/organization",
    "threadId",
    "sessionId",
    "callId",
    "progressToken",
)


def metadata_shape(params: object) -> dict[str, object]:
    """Bounded classification of incoming Events params._meta (no raw values)."""
    if not isinstance(params, dict):
        return {"params_type": "other"}
    meta = params.get("_meta")
    fields = {}
    if isinstance(meta, dict):
        for key in _META_KNOWN_KEYS:
            if key in meta:
                value = meta[key]
                fields[key] = (
                    "string" if isinstance(value, str)
                    else "null" if value is None
                    else "object" if isinstance(value, dict)
                    else "array" if isinstance(value, list)
                    else "other"
                )
    return {
        "params_type": "object",
        "meta_type": (
            "missing" if "_meta" not in params
            else "object" if isinstance(meta, dict)
            else "null" if meta is None
            else "other"
        ),
        "recognized_fields": fields,
        "other_meta_keys": min(
            sum(key not in _META_KNOWN_KEYS for key in meta), 100
        ) if isinstance(meta, dict) else 0,
        "top_level_meta_present": "meta" in params,
        "arguments_session_number_present": (
            isinstance(params.get("arguments"), dict)
            and "session_number" in params["arguments"]
        ),
    }


def emit_metadata_shape(method: str, params: object, cid: str, *, enabled=False) -> None:
    """Opt-in event subscription diagnostics, independent of identity resolution."""
    if not diagnostics_enabled(enabled) or method not in {
        "events/subscribe", "events/unsubscribe"
    }:
        return
    try:
        values = {
            "correlation_id": (
                cid if re.fullmatch(r"ev_[a-f0-9]{16}", cid) else "unknown"
            ),
            "method": method,
            **metadata_shape(params),
        }
        _LOG.info("tmcp_events_metadata %s", json.dumps(values, separators=(",", ":")))
    except Exception:
        # Never let diagnostics affect subscription results.
        pass


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


# Exact generic page wording only: this is not attribution of the rejecting service.
_HTML_MARKERS = {
    "enable javascript and cookies to continue": "browser_challenge",
    "checking your browser": "browser_challenge",
    "sorry, you have been blocked": "block_page",
    "access denied": "access_denied",
    "403 forbidden": "access_denied",
    "internal server error": "error_page",
    "bad gateway": "error_page",
    "service unavailable": "error_page",
}
_HTML_MARKER_CATEGORIES = ("browser_challenge", "block_page", "access_denied", "error_page")


class _HTMLMarkerParser(HTMLParser):
    """Match text nodes; never inspect attributes, comments, or executable text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.markers: set[str] = set()
        self.ignored: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "template"}:
            self.ignored.append(tag)

    def handle_startendtag(self, tag, attrs):
        # HTML ignores self-closing slashes on non-void tags; fail closed on malformed input.
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if self.ignored and tag == self.ignored[-1]:
            self.ignored.pop()

    def handle_data(self, data):
        if not self.ignored:
            marker = _HTML_MARKERS.get(" ".join(data.lower().split()).rstrip(".!"))
            if marker:
                self.markers.add(marker)


def _classify_html_marker(body: bytes) -> str:
    parser = _HTMLMarkerParser()
    try:
        parser.feed(body.decode("utf-8", errors="replace"))
        parser.close()
    except (ValueError, AssertionError, RecursionError):
        return "unknown"
    return next((kind for kind in _HTML_MARKER_CATEGORIES if kind in parser.markers), "unknown")


def safe_callback_response_metadata(details: dict) -> dict:
    """Second allowlist at the logging boundary; never truncate arbitrary strings."""
    safe = {}
    for key, allowed in {
        "response_content_type": _RESPONSE_CONTENT_TYPES | {"other", "missing"},
        "response_body_kind": {"json", "html", "empty", "other"},
        "response_error_code": _RESPONSE_ERROR_CODES,
        "response_html_marker": set(_HTML_MARKER_CATEGORIES) | {"unknown"},
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
                details["response_html_marker"] = _classify_html_marker(body)
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


def emit_callback_probe(cid: str, details: dict, *, enabled=False) -> None:
    """Emit ONLY bounded, non-identifying callback verification metadata.

    Never persist the destination URL, challenge, response body, arbitrary headers or key.
    Only allowlisted response categories and header-presence booleans are retained.
    These fields correlate server diagnosis with the pre-existing Events CID.
    """
    if not diagnostics_enabled(enabled):
        return
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
    try:
        _LOG.info("tmcp_events_callback %s", json.dumps(values, separators=(",", ":")))
    except Exception:
        pass


_FAILURE_CODES = frozenset(
    {
        "internal_error",
        "callback_verification_failed",
        "delivery_failed",
        "authorization_unavailable",
    }
)


def emit_failure(method: str, stage: str, code: str, cid: str, *, status=None) -> None:
    """Minimal operational evidence; never log exception text or private delivery identifiers."""
    values = {
        "correlation_id": cid if re.fullmatch(r"ev_[a-f0-9]{16}", cid) else "unknown",
        "method": method if method in _METHODS or method == "events/worker" else "unknown",
        "stage": stage if stage in {"poll", "rpc", "callback", "delivery"} else "unknown",
        "code": code if code in _FAILURE_CODES else "internal_error",
    }
    if type(status) is int and 100 <= status <= 599:
        values["http_status"] = status
    try:
        _LOG.warning("tmcp_events_error %s", json.dumps(values, separators=(",", ":")))
    except Exception:
        pass


class MCPEventIngressProbe:
    """Capture Events ingress before OAuth middleware without logging request data.

    Only buffers small POST JSON-RPC requests. Forward the original byte stream to
    the existing ASGI stack, preserving signatures, authorization and tool traffic.
    """

    def __init__(self, app, *, enabled=False):
        self.app = app
        self.enabled = enabled

    async def __call__(self, scope, receive, send):
        if not diagnostics_enabled(self.enabled):
            await self.app(scope, receive, send)
            return
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
        emit(method, "ingress", "received", cid, enabled=self.enabled)

        async def response_send(message):
            if message["type"] == "http.response.start":
                emit(
                    method,
                    "http_response",
                    f"http_{int(message['status'])}",
                    cid,
                    enabled=self.enabled,
                )
            await send(message)

        try:
            await self.app(scope, replay_receive, response_send)
        except Exception:
            emit(method, "http_response", "unhandled_exception", cid, enabled=self.enabled)
            raise
