"""Events diagnostics are optional; disabling them must not change delivery semantics."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from terminal_mcp.config import Settings
from terminal_mcp.mcp import event_diagnostics
from terminal_mcp.mcp.event_server import MCPEventsAdapter


def _settings(tmp_path):
    return Settings(
        database_path=tmp_path / "web.db",
        auth_database_path=tmp_path / "auth.db",
        runtime_config_path=tmp_path / "runtime.env",
        log_path=tmp_path / "runtime.log",
        metrics_port=0,
        fleet_v1_source_enabled=False,
        fleet_v1_authority_enabled=False,
        fleet_v1_projection_enabled=False,
        fleet_v1_public_enabled=False,
        persistent_agents_enabled=False,
        legacy_agent_admission_enabled=True,
        cwd=tmp_path,
        public_base_url="https://example.test",
        auth_mode="bearer",
        bearer_tokens="safe-test-credential",
        mcp_auth_mode="",
        actions_auth_mode="",
    )


@pytest.mark.asyncio
async def test_disabled_ingress_delegates_without_reading_or_wrapping(caplog):
    receive = Mock(side_effect=AssertionError("must not pre-read the body"))
    send = Mock()
    calls = []

    async def app(scope, actual_receive, actual_send):
        calls.append((scope, actual_receive, actual_send))

    scope = {"type": "http", "method": "POST", "path": "/terminal-mcp/access/v1/mcp/"}
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        await event_diagnostics.MCPEventIngressProbe(app)(scope, receive, send)
    assert calls == [(scope, receive, send)]
    receive.assert_not_called()
    assert "mcp_events_cid" not in scope
    assert "tmcp_events" not in caplog.text


def test_broken_diagnostic_sinks_do_not_raise(monkeypatch):
    monkeypatch.setattr(event_diagnostics._LOG, "info", Mock(side_effect=RuntimeError("PRIVATE")))
    monkeypatch.setattr(
        event_diagnostics._LOG, "warning", Mock(side_effect=RuntimeError("PRIVATE"))
    )
    event_diagnostics.emit("events/list", "rpc", "ok", "ev_0123456789abcdef", enabled=True)
    event_diagnostics.emit_callback_probe(
        "ev_0123456789abcdef", {"result": "verified"}, enabled=True
    )
    event_diagnostics.emit_failure("events/worker", "poll", "internal_error", "ev_0123456789abcdef")


@pytest.mark.asyncio
async def test_stale_dispatch_gate_keeps_retry_state_and_never_calls_callback(caplog):
    item = {"event_id": "PRIVATE_EVENT", "subscription_id": "PRIVATE_SUBSCRIPTION"}
    attempts, callbacks, checks = [], [], []

    async def due_deliveries():
        return [item]

    async def finish_attempt(event, subscription, *, status):
        attempts.append((event, subscription, status))

    async def checker(delivery):
        return True

    async def dispatch_is_current(delivery):
        checks.append(delivery)
        return False

    async def sender(*args):
        callbacks.append(args)
        return 200, b""

    store = SimpleNamespace(
        due_deliveries=due_deliveries,
        finish_attempt=finish_attempt,
        dispatch_is_current=dispatch_is_current,
    )
    adapter = MCPEventsAdapter(None, store, sender=sender, access_checker=checker)
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        await adapter.deliver_due()
        await adapter.deliver_due()
    assert checks == [item, item]
    assert callbacks == []
    assert attempts == [("PRIVATE_EVENT", "PRIVATE_SUBSCRIPTION", None)] * 2
    assert "tmcp_events_diag" not in caplog.text
    assert "tmcp_events_error" not in caplog.text
