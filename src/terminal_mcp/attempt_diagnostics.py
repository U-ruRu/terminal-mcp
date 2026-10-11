"""Payload-free attempt correlation; never decides retry or idempotency policy."""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from contextlib import suppress
from contextvars import ContextVar

from terminal_mcp.trace import current_upstream_request_id, uuid7

_SAFE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,120}\Z")
_COMMAND_HASH = re.compile(r"[0-9a-f]{8}\Z")
_current_tool_attempt_id: ContextVar[str | None] = ContextVar("mcp_tool_attempt_id", default=None)


def emit_attempt(events, event: str, **fields) -> None:
    """A broken diagnostic sink must never change an operation's outcome."""
    if events is None:
        return
    with suppress(Exception):
        upstream = current_upstream_request_id.get()
        # Do not repeat arbitrary raw caller headers in these diagnostic records.
        fingerprint = hashlib.sha256(upstream.encode()).hexdigest()[:24] if upstream else None
        events.emit(
            event, upstream_request_id=None, upstream_request_fingerprint=fingerprint, **fields
        )


def result_references(result) -> dict:
    """Whitelist receipt identifiers, never arguments, output, text or credentials."""
    value = result if isinstance(result, dict) else getattr(result, "structuredContent", None)
    if not isinstance(value, dict):
        return {}
    fields = {"result_ok": value.get("ok") if isinstance(value.get("ok"), bool) else None}
    command = value.get("command")
    command = command if isinstance(command, dict) else value
    cmd_hash = command.get("cmd_hash")
    if isinstance(cmd_hash, str) and _COMMAND_HASH.fullmatch(cmd_hash):
        fields["cmd_hash"] = cmd_hash
    task = value.get("task")
    task = task if isinstance(task, dict) else value
    task_id = task.get("task_id")
    if isinstance(task_id, str) and _SAFE_ID.fullmatch(task_id):
        fields["task_id"] = task_id
    revision = task.get("revision")
    if isinstance(revision, int) and not isinstance(revision, bool):
        fields["task_revision"] = revision
    error = value.get("error")
    code = error.get("code") if isinstance(error, dict) else value.get("code")
    if isinstance(code, str) and _SAFE_ID.fullmatch(code):
        fields["error_code"] = code
    return fields


async def traced_task_mutation(application, actor, request, *, role: str, tool: str):
    """Bounded mutation-return evidence, joined to existing HTTP/tool trace IDs.

    The return timestamp is an upper bound on the database commit, not the
    instant at which a transaction committed or proof of client delivery.
    """
    events = getattr(application.service, "events", None)
    attempt = _current_tool_attempt_id.get()
    common = {"role": role, "tool": tool, "action": request.action, "tool_attempt_id": attempt}
    for name in ("namespace", "task_id"):
        value = getattr(request, name, None)
        if isinstance(value, str) and _SAFE_ID.fullmatch(value):
            common[name] = value
    started = time.monotonic()
    try:
        raw = await application.task(actor, request)
    except BaseException:
        emit_attempt(
            events,
            "mcp_task_mutation_returned",
            **common,
            mutation_outcome="unknown",
            duration_ms=round((time.monotonic() - started) * 1000, 3),
            returned=False,
        )
        raise
    # A successful return may represent an idempotent already-applied action;
    # it is not evidence of a new domain mutation or a precise commit time.
    outcome = (
        "application_ok"
        if raw.get("ok") is True
        else raw.get("outcome")
        if raw.get("outcome") in ("not_committed", "committed", "unknown")
        else "unknown"
    )
    references = result_references(raw)
    for key in common:
        references.pop(key, None)
    emit_attempt(
        events,
        "mcp_task_mutation_returned",
        **common,
        **references,
        mutation_outcome=outcome,
        duration_ms=round((time.monotonic() - started) * 1000, 3),
        returned=True,
    )
    return raw, started


def traced_task_response(application, request, raw, response, started, *, role: str, tool: str):
    """Record adapter response formation without capturing task content or request args."""
    events = getattr(application.service, "events", None)
    emit_attempt(
        events,
        "mcp_task_response_ready",
        role=role,
        tool=tool,
        action=request.action,
        tool_attempt_id=_current_tool_attempt_id.get(),
        **result_references(raw),
        response_ok=response.get("ok") if isinstance(response, dict) else None,
        duration_ms=round((time.monotonic() - started) * 1000, 3),
    )
    return response


def install_role_attempt_diagnostics(mcp, service, role: str) -> None:
    """Wrap the final role function without changing input or output schemas."""
    events = getattr(service, "events", None)
    if events is None:
        return
    for tool in mcp._tool_manager.list_tools():
        raw_fn, name = tool.fn, tool.name

        async def traced(*, _raw_fn=raw_fn, _name=name, **kwargs):
            attempt_id = uuid7()
            token = _current_tool_attempt_id.set(attempt_id)
            started = time.monotonic()
            common = {"role": role, "tool": _name, "tool_attempt_id": attempt_id}
            emit_attempt(events, "mcp_tool_started", **common)
            outcome, references, failure = "returned", {}, None
            try:
                result = await _raw_fn(**kwargs)
                # Reference extraction is observational, even for unusual adapters.
                with suppress(Exception):
                    references = result_references(result)
                outcome = "error" if references.get("result_ok") is False else "returned"
                return result
            except asyncio.CancelledError:
                outcome, failure = "cancelled", "CancelledError"
                raise
            except Exception as exc:
                outcome, failure = "exception", type(exc).__name__
                raise
            finally:
                emit_attempt(
                    events,
                    "mcp_tool_finished",
                    **common,
                    **references,
                    outcome=outcome,
                    exception_class=failure,
                    duration_ms=round((time.monotonic() - started) * 1000, 3),
                )
                _current_tool_attempt_id.reset(token)

        tool.fn = traced
