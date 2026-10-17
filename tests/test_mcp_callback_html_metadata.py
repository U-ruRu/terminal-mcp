"""Literal generic page fixtures; no live endpoint or credential is used."""

import json
import logging

import pytest

from terminal_mcp.mcp import event_diagnostics as diagnostics


@pytest.mark.parametrize(
    ("text", "marker"),
    [
        ("Enable JavaScript and cookies to continue", "browser_challenge"),
        ("Sorry, you have been blocked", "block_page"),
        ("Access denied", "access_denied"),
        ("Internal Server Error", "error_page"),
    ],
)
def test_html_reports_only_fixed_observed_marker(text, marker, caplog):
    private = (
        "whsec_fixture-secret-987654321098765432109876543210-https://private.example.invalid/token"
    )
    body = (
        "<!doctype html><html><title>private arbitrary title</title>"
        f"<h1>{text}</h1><p>{private}</p></html>"
    ).encode()
    details = diagnostics.classify_callback_response(body)
    assert details == {"response_body_kind": "html", "response_html_marker": marker}
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        diagnostics.emit_callback_probe("ev_0123456789abcdef", details, enabled=True)
    assert f'"response_html_marker":"{marker}"' in caplog.text
    for value in [
        text,
        private,
        "private arbitrary title",
        "987654321098765432109876543210",
        "<html>",
    ]:
        assert value not in caplog.text


def test_html_parser_failure_is_unknown_without_error_text(monkeypatch, caplog):
    def fail(self, body):
        raise AssertionError("private parser error token")

    monkeypatch.setattr(diagnostics._HTMLMarkerParser, "feed", fail)
    details = diagnostics.classify_callback_response(b"<html><h1>Access denied</h1>")
    assert details == {"response_body_kind": "html", "response_html_marker": "unknown"}
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        diagnostics.emit_callback_probe("ev_0123456789abcdef", details, enabled=True)
    assert "private parser error token" not in caplog.text
    assert "Access denied" not in json.dumps(details)
