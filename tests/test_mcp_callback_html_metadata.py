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


@pytest.mark.parametrize(
    "fragment",
    [
        "<title>Just a moment...</title>",
        "<!-- Access denied -->",
        "<script>Access denied</script>",
        "<style>Access denied</style>",
        "<template><h1>Access denied</h1></template>",
    ],
)
def test_unknown_or_nontext_marker_stays_unknown(fragment):
    details = diagnostics.classify_callback_response(("<html>" + fragment).encode())
    assert details == {"response_body_kind": "html", "response_html_marker": "unknown"}


def test_malformed_html_and_invalid_utf8_are_bounded_and_do_not_escape():
    details = diagnostics.classify_callback_response(b"<html>\xff<h1>Access denied")
    assert details == {"response_body_kind": "html", "response_html_marker": "access_denied"}


def test_multiple_markers_use_fixed_priority_without_service_attribution():
    body = b"<html><title>Access denied</title><h1>Sorry, you have been blocked</h1>"
    assert diagnostics.classify_callback_response(body)["response_html_marker"] == "block_page"
    body += b"<p>Enable JavaScript and cookies to continue</p>"
    assert diagnostics.classify_callback_response(body) == {
        "response_body_kind": "html",
        "response_html_marker": "browser_challenge",
    }


def test_nonhtml_is_not_classified_from_headers_or_text():
    body = b"Access denied"
    details = diagnostics.classify_callback_response(body, {"content-type": "text/html"}.get)
    assert "response_html_marker" not in details


def test_html_classifier_respects_64k_byte_boundary_before_parsing(monkeypatch):
    body = b"<html><h1>Access denied</h1>".ljust(65536, b" ")
    assert diagnostics.classify_callback_response(body)["response_html_marker"] == "access_denied"
    monkeypatch.setattr(diagnostics, "_classify_html_marker", lambda body: pytest.fail("parsed"))
    assert diagnostics.classify_callback_response(body + b" ") == {}


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
