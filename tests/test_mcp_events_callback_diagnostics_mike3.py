"""Safe callback-verification diagnostics for the platform-owned webhook."""

import base64
import json
import logging

import pytest

from terminal_mcp.mcp.event_diagnostics import emit_callback_probe
from terminal_mcp.mcp.event_webhooks import CallbackEndpointError, verify_callback

SECRET = "whsec_" + base64.b64encode(b"x" * 32).decode("ascii")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("variant", "expected"),
    [
        ("success", "verified"),
        ("http_error", "http_rejected"),
        ("invalid_json", "invalid_json"),
        ("missing", "challenge_missing"),
        ("not_string", "challenge_not_string"),
        ("mismatch", "challenge_mismatch"),
    ],
)
async def test_callback_verification_observations_are_safe(variant, expected, caplog):
    recorded = []
    private_challenge = None

    async def receiver(url, body, headers):
        nonlocal private_challenge
        assert url == "https://private.example.test/secret-path"
        private_challenge = json.loads(body)["challenge"]
        assert headers["X-MCP-Subscription-Id"] == "sub_test"
        if variant == "http_error":
            return 403, b"not authorized"
        if variant == "invalid_json":
            return 200, b"{"
        if variant == "missing":
            return 200, b'{"other": "none"}'
        if variant == "not_string":
            return 200, b'{"challenge": 123}'
        if variant == "mismatch":
            return 200, b'{"challenge": "incorrect"}'
        return 200, json.dumps({"challenge": private_challenge}).encode()

    def safe_observation(observation):
        recorded.append(observation)
        emit_callback_probe("ev_0123456789abcdef", observation)

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        if variant == "success":
            await verify_callback(
                "https://private.example.test/secret-path",
                SECRET,
                "sub_test",
                sender=receiver,
                observer=safe_observation,
            )
        else:
            with pytest.raises(CallbackEndpointError) as exc:
                await verify_callback(
                    "https://private.example.test/secret-path",
                    SECRET,
                    "sub_test",
                    sender=receiver,
                    observer=safe_observation,
                )
            assert exc.value.reason == "challenge_failed"

    assert len(recorded) == 1
    metadata = recorded[0]
    assert metadata["result"] == expected
    assert metadata["http_status"] == (403 if variant == "http_error" else 200)
    assert metadata["elapsed_ms"] >= 0
    assert metadata["body_size"] >= 1
    if variant == "success":
        assert metadata["challenge_matches"] is True
    if variant == "mismatch":
        assert metadata["challenge_matches"] is False
    log_text = caplog.text
    assert '"result":"' + expected + '"' in log_text
    assert "private.example.test" not in log_text
    assert "secret-path" not in log_text
    assert SECRET not in log_text
    assert private_challenge not in log_text
    assert "sub_test" not in log_text


@pytest.mark.asyncio
async def test_transport_error_reports_safe_category_and_preserves_failure(caplog):
    recorded = []

    async def sender(url, body, headers):
        raise CallbackEndpointError("transport_error")

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        with pytest.raises(CallbackEndpointError) as exc:
            await verify_callback(
                "https://receiver.example.test/path",
                SECRET,
                "sub_test",
                sender=sender,
                observer=lambda data: (
                    recorded.append(data),
                    emit_callback_probe("ev_0123456789abcdef", data),
                ),
            )
    assert exc.value.reason == "transport_error"
    assert recorded[0]["http_status"] is None
    assert recorded[0]["body_size"] is None
    assert '"result":"transport_error"' in caplog.text
