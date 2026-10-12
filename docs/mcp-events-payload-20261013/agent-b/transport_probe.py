"""Capture real adapter/serializer bytes at its injected sender boundary.

No network, production database, subscriptions, or credentials are used. The
receiver and authorization predicate are synthetic test doubles, not live proof.
Run from the checkout with PYTHONPATH=src and the project's test environment.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
from unittest.mock import patch

from terminal_mcp.mcp.event_server import MCPEventsAdapter

KEY = bytes(range(32))  # Public synthetic fixture; never a production credential.
SECRET = "whsec_" + base64.b64encode(KEY).decode("ascii")
STAMP = "2026-10-12T21:00:00.000Z"


class FixtureStore:
    def __init__(self) -> None:
        self.items: list[dict] = []
        self.receipts: list[dict] = []

    async def due_deliveries(self) -> list[dict]:
        current, self.items = self.items, []
        return current

    async def dispatch_is_current(self, item: dict) -> bool:
        return True

    async def finish_attempt(self, event_id: str, sub_id: str, *, status=None) -> None:
        self.receipts.append({"eventId": event_id, "subscription_id": sub_id, "status": status})


def item(index: int) -> dict:
    data = {
        "server": "synthetic-node", "sender": "Probe-Sender",
        "recipient": "Probe-Recipient", "message_id": f"synthetic:message:{index}",
        "text": f'Контроль {index}: "кавычки", перенос\nстроки, Unicode ✓',
        "timestamp": STAMP,
    }
    return {
        "event_id": f"evt_synthetic_{index}", "name": "message.received",
        "occurred_at": STAMP, "data_json": json.dumps(data, ensure_ascii=False),
        "callback_url": "https://receiver.example.test/events",
        "signing_secret": SECRET, "subscription_id": "sub_synthetic",
        "rotation_until": None, "previous_signing_secret": None,
    }


async def run() -> dict:
    captures: list[dict] = []
    store = FixtureStore()

    async def allow_fixture_only(value: dict) -> bool:
        return value["subscription_id"] == "sub_synthetic"

    async def capture(url: str, body: bytes, headers: dict) -> tuple[int, bytes]:
        decoded = json.loads(body)
        assert isinstance(decoded, dict), "HTTP arrays are not the ChatGPT batch contract"
        assert set(decoded) == {"eventId", "name", "timestamp", "data", "cursor"}
        assert isinstance(decoded["data"], dict), "data must not be double-serialized"
        assert decoded["cursor"] is None
        assert headers["Content-Type"] == "application/json"
        assert headers["webhook-id"] == decoded["eventId"]
        assert headers["X-MCP-Subscription-Id"] == "sub_synthetic"
        signed = headers["webhook-id"].encode() + b"." + headers["webhook-timestamp"].encode() + b"." + body
        expected = "v1," + base64.b64encode(hmac.new(KEY, signed, hashlib.sha256).digest()).decode()
        assert hmac.compare_digest(headers["webhook-signature"], expected)
        safe_headers = dict(headers)
        safe_headers["webhook-signature"] = "<redacted:verified>"
        captures.append({
            "capture_kind": "injected_sender_boundary_not_HTTP",
            "request": {"method": "POST", "callback_host": "receiver.example.test",
                        "headers": safe_headers, "body_utf8": body.decode("utf-8"),
                        "body_sha256": hashlib.sha256(body).hexdigest(), "body_bytes": len(body)},
            "response": {"status": 200, "body_utf8": "", "origin": "synthetic_receiver"},
        })
        return 200, b""

    adapter = MCPEventsAdapter(None, store, sender=capture, access_checker=allow_fixture_only)
    with patch("terminal_mcp.mcp.event_webhooks.time.time", return_value=1791838800):
        store.items = [item(1)]
        await adapter.deliver_due()
        assert len(captures) == 1
        store.items = [item(2), item(3)]
        await adapter.deliver_due()
    assert len(captures) == 3
    assert len({json.loads(c["request"]["body_utf8"])["eventId"] for c in captures}) == 3
    assert len(store.receipts) == 3 and all(r["status"] == 200 for r in store.receipts)
    return {"result": "PASS", "live_delivery_proven": False,
            "cases": ["singleton", "two_due_events_are_two_requests", "UTF-8_object_data_exact_bytes_signature"],
            "captures": captures, "synthetic_receipts": store.receipts}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(run())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"result": result["result"], "captures": len(result["captures"]),
                      "live_delivery_proven": False, "output": str(args.output)}))
