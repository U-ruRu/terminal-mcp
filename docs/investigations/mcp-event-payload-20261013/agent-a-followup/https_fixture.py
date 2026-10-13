"""Small differential probe: real loopback TLS, no production state or callback.

Production serializer, signer and HTTPS code execute unchanged. Test-only DNS,
IP classification and socket facades map one RFC 5737 address to this process's
loopback TLS listener. Only the fixture admits that address as public.
TLS chain and hostname checks stay enabled with a temporary test certificate.
This proves wire equivalence, not public DNS, ChatGPT ingestion or model visibility.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from terminal_mcp.mcp import event_webhooks as transport
from terminal_mcp.mcp.event_server import MCPEventsAdapter

HERE = Path(__file__).resolve().parent
HOST = "receiver.tmcp-fixture.invalid"
KEY = bytes(range(32))  # Public synthetic test key, never a production credential.
SECRET = "whsec_" + base64.b64encode(KEY).decode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class Store:
    def __init__(self, items):
        self.items, self.receipts = items, []

    async def due_deliveries(self):
        items, self.items = self.items, []
        return items

    async def dispatch_is_current(self, item):
        return True

    async def finish_attempt(self, event_id, sub_id, *, status=None):
        self.receipts.append({"eventId": event_id, "status": status})


async def native(events, url):
    rows = [
        {
            "event_id": e["eventId"],
            "name": e["name"],
            "occurred_at": e["timestamp"],
            "data_json": json.dumps(e["data"], ensure_ascii=False),
            "callback_url": url,
            "subscription_id": "sub_fixture",
            "signing_secret": SECRET,
            "rotation_until": None,
            "previous_signing_secret": None,
        }
        for e in events
    ]
    store = Store(rows)

    async def synthetic_scope_only(item):
        return item["subscription_id"] == "sub_fixture"

    adapter = MCPEventsAdapter(None, store, access_checker=synthetic_scope_only)
    await adapter.deliver_due()
    assert len(store.receipts) == len(events) and all(r["status"] == 200 for r in store.receipts)
    return store.receipts


def run(source_root: str) -> dict:
    received = []
    signed_at = int(time.time())
    stamp = (
        datetime.fromtimestamp(signed_at, UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    texts = [
        "single ASCII control",
        'Кавычки "тест", перенос\nстроки, Unicode ✓ 😀',
        "batch-second / slash \\ tab\tend",
    ]
    events = [
        {
            "eventId": f"evt_fixture_{i}",
            "name": "message.received",
            "timestamp": stamp,
            "data": {
                "server": "fixture",
                "sender": "Fixture-Sender",
                "recipient": "Fixture-Recipient",
                "message_id": f"fixture:msg:{i}",
                "text": text,
                "timestamp": stamp,
            },
            "cursor": None,
        }
        for i, text in enumerate(texts, 1)
    ]

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            size = int(self.headers["Content-Length"])
            assert size <= 262144
            body = self.rfile.read(size)
            headers = {k.lower(): v for k, v in self.headers.items()}
            value = json.loads(body)
            expected = (
                "v1,"
                + base64.b64encode(
                    hmac.new(
                        KEY,
                        (headers["webhook-id"] + "." + headers["webhook-timestamp"] + ".").encode()
                        + body,
                        hashlib.sha256,
                    ).digest()
                ).decode()
            )
            valid = hmac.compare_digest(headers["webhook-signature"], expected)
            assert (
                valid
                and headers["webhook-id"] == value["eventId"]
                and headers["x-mcp-subscription-id"] == "sub_fixture"
            )
            received.append(
                {
                    "body": body,
                    "headers": headers,
                    "signature_verified": valid,
                    "tls_version": self.connection.version(),
                }
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    with tempfile.TemporaryDirectory(prefix="tmcp-events-tls-") as td:
        td = Path(td)
        cert, key = td / "cert.pem", td / "key.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-subj",
                "/CN=" + HOST,
                "-addext",
                "subjectAltName=DNS:" + HOST,
                "-keyout",
                str(key),
                "-out",
                str(cert),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        port = server.server_address[1]
        url = f"https://{HOST}:{port}/events"
        real_connect = socket.create_connection
        create_context = ssl.create_default_context

        def fixture_dns(host, requested_port, **kwargs):
            assert host == HOST and requested_port == port
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.7", port))]

        def fixture_connect(address, **kwargs):
            assert address == ("203.0.113.7", port)
            return real_connect(("127.0.0.1", port), **kwargs)

        socket_facade = SimpleNamespace(
            SOCK_STREAM=socket.SOCK_STREAM,
            getaddrinfo=fixture_dns,
            create_connection=fixture_connect,
        )
        ssl_facade = SimpleNamespace(
            SSLError=ssl.SSLError, create_default_context=lambda: create_context(cafile=str(cert))
        )
        assert not transport.ipaddress.ip_address("203.0.113.7").is_global
        ip_facade = SimpleNamespace(
            ip_address=lambda value: SimpleNamespace(is_global=value == "203.0.113.7")
        )
        try:
            with (
                patch.object(transport, "ipaddress", ip_facade),
                patch.object(transport, "socket", socket_facade),
                patch.object(transport, "ssl", ssl_facade),
                patch.object(transport.time, "time", return_value=signed_at),
            ):
                # One singleton delivery, then two due events in one worker pass.
                receipts = asyncio.run(native(events[:1], url)) + asyncio.run(
                    native(events[1:], url)
                )
            cfg = {
                "host": HOST,
                "port": port,
                "signedAt": signed_at,
                "secret": SECRET,
                "ca": str(cert),
                "sourceRoot": source_root,
                "reclyFile": str(
                    Path(source_root) / "rokrokss/recly/experiments/dot-probe/probe.mjs"
                ),
                "events": events,
            }
            result = subprocess.run(
                ["node", str(HERE / "fixture_senders.mjs")],
                input=json.dumps(cfg, ensure_ascii=False),
                text=True,
                capture_output=True,
                timeout=25,
                check=True,
            )
            node = json.loads(result.stdout)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)
    assert len(received) == 6
    comparisons = []
    for i in range(3):
        py, js = received[i], received[i + 3]
        assert py["body"] == js["body"]
        for header in [
            "content-type",
            "webhook-id",
            "webhook-timestamp",
            "webhook-signature",
            "x-mcp-subscription-id",
            "content-length",
        ]:
            assert py["headers"][header] == js["headers"][header], header
        assert json.loads(py["body"]) == events[i]
        comparisons.append(
            {
                "eventId": events[i]["eventId"],
                "body_bytes": len(py["body"]),
                "body_sha256": digest(py["body"]),
                "body_and_application_headers_identical": True,
                "python_only_headers": sorted(set(py["headers"]) - set(js["headers"])),
                "node_only_headers": sorted(set(js["headers"]) - set(py["headers"])),
            }
        )
    captures = []
    for i, c in enumerate(received):
        safe = dict(c["headers"])
        safe["webhook-signature"] = "<redacted:verified>"
        captures.append(
            {
                "sender": "Terminal MCP" if i < 3 else "OpenAI documented Node sendEvent",
                "request": {
                    "method": "POST",
                    "host": HOST,
                    "path": "/events",
                    "headers": safe,
                    "body_utf8": c["body"].decode(),
                    "body_sha256": digest(c["body"]),
                },
                "response": {
                    "status": 200,
                    "body_utf8": "{}",
                    "origin": "isolated_loopback_fixture",
                },
                "tls_version": c["tls_version"],
                "signature_verified": c["signature_verified"],
            }
        )
    return {
        "result": "PASS",
        "captured_at": datetime.now(UTC).isoformat(),
        "scope": "real loopback TLS with fixture-only address mapping; no production callback",
        "production_mutations": False,
        "live_model_visibility_proven": False,
        "subject_git_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "sender_source_sha256": digest(Path(transport.__file__).read_bytes()),
        "cases": ["single_ASCII", "Unicode_quotes_newline", "two_due_events_two_requests"],
        "request_count": len(received),
        "comparisons": comparisons,
        "public_signers": node,
        "native_receipts": receipts,
        "captures": captures,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sources", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    evidence = run(args.sources)
    Path(args.output).write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {k: v for k, v in evidence.items() if k not in {"captures", "native_receipts"}},
            ensure_ascii=False,
        )
    )
