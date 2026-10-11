"""No live endpoint or production credential is used by these fixtures."""

import base64
import hashlib
import hmac
import json
import logging
import socket
from unittest.mock import Mock

import pytest

from terminal_mcp.mcp import event_webhooks as webhooks
from terminal_mcp.mcp.event_diagnostics import (
    classify_callback_response,
    emit_callback_probe,
)

SECRET = "whsec_AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
REQUEST_ID = "12345678-abcd-abcd-abcd-123456789abc"


def test_response_allowlist_and_redaction(caplog):
    private = "private-url-cookie-token-challenge-value"
    headers = {
        "content-type": "text/html; secret=" + private,
        "cf-ray": "1234567890abcdef-SJC",
        "x-request-id": REQUEST_ID,
        "cf-mitigated": "challenge",
        "set-cookie": private,
        "location": "https://private.example/" + private,
        "authorization": "Bearer " + private,
    }
    details = classify_callback_response(
        ("<!DOCTYPE html><html>" + private + "</html>").encode(), headers.get
    )
    assert details == {
        "response_body_kind": "html",
        "response_content_type": "text/html",
        "response_cf_challenge": True,
        "response_cf_ray_present": True,
        "response_request_id_present": True,
    }
    details.update({"result": "http_rejected", "http_status": 403, "raw": private})
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        emit_callback_probe("ev_0123456789abcdef", details)
    assert private not in caplog.text
    assert REQUEST_ID not in caplog.text
    assert "1234567890abcdef-SJC" not in caplog.text
    assert "set-cookie" not in caplog.text


@pytest.mark.parametrize(
    "value", ["secret", "x" * 10000, REQUEST_ID + "\r\nsecret", "https://x.test/a", {}, None]
)
def test_untrusted_identifier_values_are_dropped(value, caplog):
    details = {
        "response_request_id": value,
        "response_cf_ray": value,
        "response_error_code": value,
        "callback_explicit_default_port": value,
    }
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        emit_callback_probe("ev_0123456789abcdef", details)
    assert "response_request_id" not in caplog.text
    assert "response_cf_ray" not in caplog.text
    assert "response_error_code" not in caplog.text
    assert "callback_explicit_default_port" not in caplog.text


@pytest.mark.parametrize(
    ("body", "kind", "code"),
    [
        (
            b'{"error":{"code":"invalid_signature","message":"private"}}',
            "json",
            "invalid_signature",
        ),
        (b'{"code":"timestamp_expired","token":"private"}', "json", "timestamp_expired"),
        (b'{"code":"private"}', "json", None),
        (b'["private"]', "json", None),
        (b"<!doctype html>private invalid_signature", "html", None),
        (b"private invalid_signature", "other", None),
        (b"", "empty", None),
    ],
)
def test_body_classification_never_copies_text(body, kind, code):
    details = classify_callback_response(body)
    assert details["response_body_kind"] == kind
    assert details.get("response_error_code") == code
    assert "private" not in json.dumps(details)


def install_fake_https(monkeypatch, response):
    connection = Mock()
    connection.getresponse.return_value = response
    monkeypatch.setattr(
        webhooks.socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    )
    monkeypatch.setattr(webhooks.socket, "create_connection", Mock())
    monkeypatch.setattr(webhooks.http.client, "HTTPSConnection", lambda *a, **k: connection)
    return connection


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("authority", "explicit"), [("callback.example", False), ("callback.example:443", True)]
)
async def test_full_rejected_callback_metadata(monkeypatch, authority, explicit, caplog):
    response = Mock(status=403)
    response.getheader.side_effect = {"content-type": "text/html", "x-request-id": REQUEST_ID}.get
    response.read.return_value = b"<!doctype html><html>private response</html>"
    connection = install_fake_https(monkeypatch, response)
    recorded = []

    def observe(details):
        recorded.append(details)
        emit_callback_probe("ev_0123456789abcdef", details)

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        with pytest.raises(webhooks.CallbackEndpointError, match="challenge_failed"):
            await webhooks.verify_callback(
                "https://" + authority + "/private", SECRET, "sub_private", observer=observe
            )
    assert recorded[0]["response_request_id_present"] is True
    assert "response_request_id" not in recorded[0]
    assert recorded[0]["callback_explicit_default_port"] is explicit
    assert recorded[0]["response_body_kind"] == "html"
    assert recorded[0]["http_status"] == 403
    for private in [authority, "/private", "private response", SECRET, "sub_private"]:
        assert private not in caplog.text
    connection.close.assert_called_once()
    connection.request.assert_called_once()
    response.read.assert_called_once_with(65537)


def test_transport_diagnostic_failure_does_not_change_result(monkeypatch):
    response = Mock(status=200)
    response.getheader.return_value = None
    response.read.return_value = b"{}"
    connection = install_fake_https(monkeypatch, response)
    monkeypatch.setattr(
        webhooks, "classify_callback_response", Mock(side_effect=RuntimeError("private"))
    )
    assert webhooks._pinned_https_post("https://callback.example/private", b"{}", {}, {}) == (
        200,
        b"{}",
    )
    connection.close.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {
            "eventId": "evt_demo_001",
            "name": "demo.item.created",
            "timestamp": "2030-01-01T00:00:00Z",
            "data": {"source_id": "demo-source", "item_id": "demo-item-001"},
            "cursor": None,
        },
        {"type": "verification", "challenge": "public-fixture-challenge"},
    ],
)
async def test_signing_demo_raw_bytes_vector(monkeypatch, payload):
    monkeypatch.setattr(webhooks.time, "time", lambda: 1893456000)
    expected_body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    prefix = b"evt_demo_001.1893456000."
    expected_signature = "v1," + base64.b64encode(
        hmac.digest(bytes(range(32)), prefix + expected_body, hashlib.sha256)
    ).decode("ascii")

    async def receiver(url, body, headers):
        assert body == expected_body
        assert headers["webhook-signature"] == expected_signature
        assert headers["webhook-id"] == "evt_demo_001"
        assert headers["webhook-timestamp"] == "1893456000"
        # Independent stdlib verification of exact bytes; no runtime freshness change.
        actual = base64.b64decode(headers["webhook-signature"][3:], validate=True)
        assert hmac.compare_digest(actual, hmac.digest(bytes(range(32)), prefix + body, "sha256"))
        assert not hmac.compare_digest(
            actual, hmac.digest(bytes(range(32)), prefix + body + b" ", "sha256")
        )
        return 200, b"{}"

    assert await webhooks.post_signed(
        "https://fixture.invalid", SECRET, "sub_demo", "evt_demo_001", payload, sender=receiver
    ) == (200, b"{}")


@pytest.mark.parametrize("body", [b"x" * 65537, "not bytes", None])
def test_classifier_rejects_unbounded_or_nonbyte_input(body):
    assert classify_callback_response(body) == {}


def test_id_shaped_secret_not_retained(caplog):
    secret_like = "0123456789abcdef0123456789abcdef"
    details = classify_callback_response(
        b"{}", {"x-request-id": secret_like, "cf-ray": "0123456789abcdef-SJC"}.get
    )
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        emit_callback_probe("ev_0123456789abcdef", details)
    assert secret_like not in caplog.text
    assert "0123456789abcdef-SJC" not in caplog.text
    assert details["response_request_id_present"] is True
