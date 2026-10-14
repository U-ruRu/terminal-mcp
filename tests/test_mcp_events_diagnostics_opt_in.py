"""Events diagnostics are optional; disabling them must not change delivery semantics."""

from __future__ import annotations

import base64
import json
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from terminal_mcp.app import create_app
from terminal_mcp.config import Settings
from terminal_mcp.mcp import event_diagnostics, event_webhooks
from terminal_mcp.mcp.event_server import MCPEventsAdapter
from terminal_mcp.runtime import RuntimeConfig, RuntimeConfigProvider


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


@pytest.mark.asyncio
async def test_runtime_flag_changes_without_reconstructing_probe(tmp_path, caplog):
    path = tmp_path / "runtime.env"
    provider = RuntimeConfigProvider(path)
    received = []

    async def app(scope, receive, send):
        received.append((await receive())["body"])
        await send({"type": "http.response.start", "status": 403, "headers": []})

    probe = event_diagnostics.MCPEventIngressProbe(
        app, enabled=lambda: provider.current.detailed_logging
    )
    body = json.dumps({"method": "events/subscribe", "secret": "PRIVATE_BODY"}).encode()

    async def invoke():
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            pass

        await probe({"type": "http", "method": "POST", "path": "/mcp/"}, receive, send)

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        assert RuntimeConfig().detailed_logging is False
        await invoke()
        assert "tmcp_events_diag" not in caplog.text
        path.write_text("TERMINAL_MCP_DETAILED_LOGGING=true\n")
        await provider.refresh(force=True)
        await invoke()
        assert '"stage":"ingress"' in caplog.text
        assert '"code":"http_403"' in caplog.text
        assert "PRIVATE_BODY" not in caplog.text
        caplog.clear()
        path.write_text("TERMINAL_MCP_DETAILED_LOGGING=false\n")
        await provider.refresh(force=True)
        await invoke()
        assert "tmcp_events_diag" not in caplog.text
    assert received == [body, body, body]


def test_logging_boundaries_default_off_and_provider_failure_closed(caplog):
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        event_diagnostics.emit("events/list", "rpc", "ok", "ev_0123456789abcdef")
        event_diagnostics.emit_callback_probe("ev_0123456789abcdef", {"result": "verified"})
        event_diagnostics.emit(
            "events/list",
            "rpc",
            "ok",
            "ev_0123456789abcdef",
            enabled=Mock(side_effect=RuntimeError("PRIVATE_CONFIGURATION")),
        )
    assert "tmcp_events" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("detailed", [False, True])
async def test_unsubscribe_receipt_opt_in_after_committed_removal(detailed, caplog, monkeypatch):
    class Store:
        committed = False

        async def unsubscribe_matching(self, principal, name, callback, arguments):
            self.committed = True

    store = Store()
    adapter = MCPEventsAdapter(None, store, detailed_logging=detailed)
    monkeypatch.setattr(adapter, "_identity", lambda: "PRIVATE_PRINCIPAL")
    observed_commit = []
    original = event_diagnostics._LOG.info

    def record_commit(*args, **kwargs):
        if '"code":"unsubscribed"' in args[-1]:
            observed_commit.append(store.committed)
        return original(*args, **kwargs)

    monkeypatch.setattr(event_diagnostics._LOG, "info", record_commit)
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        result = await adapter.handle(
            {
                "method": "events/unsubscribe",
                "id": 1,
                "params": {
                    "name": "agent.attached",
                    "arguments": {},
                    "delivery": {
                        "mode": "webhook",
                        "url": "https://private.example.test/private-path",
                    },
                },
            }
        )
    assert result["result"] == {}
    assert store.committed
    assert observed_commit == ([True] if detailed else [])
    assert ("tmcp_events_diag" in caplog.text) is detailed
    assert "PRIVATE_PRINCIPAL" not in caplog.text
    assert "private.example.test" not in caplog.text
    assert "private-path" not in caplog.text


@pytest.mark.asyncio
async def test_no_callback_response_classification_without_observer(monkeypatch):
    async def post(url, secret, subscription_id, webhook_id, payload, **kwargs):
        assert kwargs["response_metadata"] is None
        return 200, json.dumps({"challenge": payload["challenge"]}).encode()

    classify = Mock(side_effect=AssertionError("unexpected diagnostic classification"))
    monkeypatch.setattr(event_webhooks, "post_signed", post)
    monkeypatch.setattr(event_webhooks, "classify_callback_response", classify)
    await event_webhooks.verify_callback("https://receiver.example.test", "PRIVATE_KEY", "private")
    classify.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [None, 503, 410])
async def test_default_minimal_failure_logs_preserve_retry_status(status, caplog):
    item = {
        "event_id": "PRIVATE_EVENT",
        "subscription_id": "PRIVATE_SUBSCRIPTION",
        "name": "agent.attached",
        "occurred_at": "2026-01-01T00:00:00Z",
        "data_json": json.dumps({"private": "PRIVATE_PAYLOAD"}),
        "callback_url": "https://private.example.test/private-path",
        "signing_secret": "whsec_"
        + base64.b64encode(b"PRIVATE_SIGNING_KEY_VALUE_32_BYTES").decode(),
        "rotation_until": None,
    }
    attempts = []

    async def due_deliveries():
        return [item]

    async def finish_attempt(event, subscription, *, status):
        attempts.append(status)

    async def checker(item):
        return True

    async def sender(url, body, headers):
        if status is None:
            raise RuntimeError("PRIVATE_EXCEPTION_WITH_CREDENTIALS")
        return status, b"PRIVATE_RESPONSE"

    dispatch_checks = []

    async def dispatch_is_current(delivery):
        # This retry fixture models a current subscription lease on both attempts.
        dispatch_checks.append(delivery["event_id"])
        return True

    store = SimpleNamespace(
        due_deliveries=due_deliveries,
        finish_attempt=finish_attempt,
        dispatch_is_current=dispatch_is_current,
    )
    adapter = MCPEventsAdapter(None, store, sender=sender, access_checker=checker)
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        await adapter.deliver_due()
        await adapter.deliver_due()
    assert attempts == [status, status]
    assert dispatch_checks == ["PRIVATE_EVENT", "PRIVATE_EVENT"]
    assert caplog.text.count("tmcp_events_error") == 1
    assert '"code":"delivery_failed"' in caplog.text
    assert "tmcp_events_diag" not in caplog.text
    for private in ("PRIVATE_", "private.example.test", "private-path", "webhook-signature"):
        assert private not in caplog.text


def test_actual_asgi_default_no_event_diagnostics(tmp_path, caplog):
    app = create_app(_settings(tmp_path))
    with caplog.at_level(logging.INFO, logger="uvicorn.error"), TestClient(app) as client:
        response = client.post(
            "/terminal-mcp/access/v1/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "events/subscribe",
                "params": {"PRIVATE_PAYLOAD": "PRIVATE_SECRET"},
            },
        )
        assert response.status_code in (401, 403)
    assert "tmcp_events_diag" not in caplog.text
    assert "tmcp_events_callback" not in caplog.text
    assert "PRIVATE_" not in caplog.text


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


def test_metadata_shape_logging_is_switchable_and_never_exposes_values(caplog):
    probe = event_diagnostics.emit_metadata_shape
    params = {
        "name": "message.received",
        "arguments": {"session_number": "1234"},
        "_meta": {
            "openai/subject": "SECRET-USER-SUBJECT",
            "openai/session": "SECRET-CONVERSATION",
            "custom-field": "SECRET-OTHER-VALUE",
            "threadId": None,
        },
    }
    enabled = {"value": False}
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        probe("events/subscribe", params, "ev_" + "a" * 16, enabled=lambda: enabled["value"])
        assert "tmcp_events_metadata" not in caplog.text
        enabled["value"] = True
        probe("events/subscribe", params, "ev_" + "a" * 16, enabled=lambda: enabled["value"])
    records = [record for record in caplog.records if "tmcp_events_metadata" in record.message]
    assert len(records) == 1
    payload = json.loads(records[0].message.split("tmcp_events_metadata ", 1)[1])
    assert payload["recognized_fields"] == {
        "openai/subject": "string",
        "openai/session": "string",
        "threadId": "null",
    }
    assert payload["meta_type"] == "object"
    assert payload["other_meta_keys"] == 1
    assert payload["arguments_session_number_present"] is True
    for forbidden in ("SECRET-USER-SUBJECT", "SECRET-CONVERSATION", "SECRET-OTHER-VALUE", "1234"):
        assert forbidden not in caplog.text
    assert event_diagnostics.metadata_shape({})["meta_type"] == "missing"
    assert event_diagnostics.metadata_shape({"_meta": None})["meta_type"] == "null"
    assert event_diagnostics.metadata_shape({"_meta": "opaque"})["meta_type"] == "other"
