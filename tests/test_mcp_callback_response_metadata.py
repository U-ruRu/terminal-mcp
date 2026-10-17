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
        "response_html_marker": "unknown",
        "response_content_type": "text/html",
        "response_cf_challenge": True,
        "response_cf_ray_present": True,
        "response_request_id_present": True,
    }
    details.update({"result": "http_rejected", "http_status": 403, "raw": private})
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        emit_callback_probe("ev_0123456789abcdef", details, enabled=True)
    assert private not in caplog.text
    assert REQUEST_ID not in caplog.text
    assert "1234567890abcdef-SJC" not in caplog.text
    assert "set-cookie" not in caplog.text


@pytest.mark.parametrize(
    ("body", "kind", "code"),
    [
        (
            b'{"error":{"code":"invalid_signature","message":"private"}}',
            "json",
            "invalid_signature",
        ),
        (b'{"code":"private"}', "json", None),
        (b'["private"]', "json", None),
        (b"<!doctype html>private invalid_signature", "html", None),
        (b"private invalid_signature", "other", None),
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
    "payload",
    [
        {
            "eventId": "evt_demo_001",
            "name": "demo.item.created",
            "timestamp": "2030-01-01T00:00:00Z",
            "data": {"source_id": "demo-source", "item_id": "demo-item-001"},
            "cursor": None,
        },
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
