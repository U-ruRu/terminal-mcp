"""Public session-number routing in the existing authorized server space."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_mcp_events_message_binding import KEY, URL, config, meta, rpc, tool

from terminal_mcp.app import create_app
from terminal_mcp.core.orchestration import utc_text
from terminal_mcp.core.persistent_admission import VerifiedAdmissionContext
from terminal_mcp.mcp.event_store import event_catalog


class Sink:
    def __init__(self):
        self.requests = []

    async def __call__(self, url, raw_body, headers):
        data = json.loads(raw_body)
        self.requests.append((url, data, headers))
        if data.get("type") == "verification":
            return 200, json.dumps({"challenge": data["challenge"]}).encode()
        return 202, b""


def register(client, number, *, token="test-bearer-a", url=URL, evidence=None):
    return rpc(
        client, "access", "events/subscribe",
        {
            "name": "message.received",
            "arguments": {"session_number": number},
            "delivery": {"mode": "webhook", "url": url, "secret": KEY},
        },
        token=token, evidence=evidence,
    )


def remove(client, number, *, token="test-bearer-a", url=URL):
    return rpc(
        client, "access", "events/unsubscribe",
        {
            "name": "message.received",
            "arguments": {"session_number": number},
            "delivery": {"mode": "webhook", "url": url},
        },
        token=token,
    )


def issue(client, app, label):
    access_meta = meta(label, label + "-access")
    number = tool(
        client, "access", "session", {"action": "start"}, evidence=access_meta
    )["session_number"]
    slot = app.state.access_mesh.store.numbers.winner(number)
    coordination_meta = meta(label, label + "-coordination")
    tool(
        client, "coordinator", "session", {"session_number": number},
        evidence=coordination_meta,
    )
    return number, slot, access_meta, coordination_meta


def stored(app, sub_id):
    with sqlite3.connect(app.state.settings.database_path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT s.*,b.* FROM mcp_event_subscriptions s "
            "JOIN mcp_event_message_bindings b ON b.subscription_id=s.id WHERE s.id=?",
            (sub_id,),
        ).fetchone()
        return dict(row) if row else None


def test_number_catalog_and_other_schemas_unchanged():
    events = {item["name"]: item for item in event_catalog()["events"]}
    assert events["message.received"]["inputSchema"] == {
        "type": "object",
        "properties": {"session_number": {"type": "string", "pattern": "^[0-9]{4}$"}},
        "additionalProperties": False,
    }
    for name in ("agent.attached", "agent.inactive", "session.ended"):
        assert events[name]["inputSchema"] == {
            "type": "object", "properties": {}, "additionalProperties": False
        }


@pytest.mark.parametrize("number", [None, 1234, "123", "12345", "１２３４", "12a4", " 1234"])
def test_invalid_number_rejected_before_challenge(tmp_path, number):
    app = create_app(config(tmp_path))
    sink = Sink()
    app.state.access_events.sender = sink
    with TestClient(app, base_url="https://terminal.example") as client:
        result = register(client, number)
        assert result["error"]["code"] == -32602
        assert sink.requests == []


def test_unissued_number_and_unauthenticated_request_rejected(tmp_path):
    app = create_app(config(tmp_path))
    sink = Sink()
    app.state.access_events.sender = sink
    with TestClient(app, base_url="https://terminal.example") as client:
        assert register(client, "1234")["error"]["code"] == -32001
        response = client.post(
            "/terminal-mcp/access/v1/mcp/",
            json={"jsonrpc": "2.0", "id": 1, "method": "events/subscribe",
                  "params": {"name": "message.received",
                             "arguments": {"session_number": "1234"},
                             "delivery": {"mode": "webhook", "url": URL, "secret": KEY}}},
            headers={"authorization": "Bearer unrelated-unauthorized-token"},
        )
        assert response.status_code == 401
        assert sink.requests == []


def test_shared_number_fanout_exact_recipient_refresh_and_selective_unsubscribe(tmp_path):
    app = create_app(config(tmp_path))
    sink = Sink()
    app.state.access_events.sender = sink
    app.state.mcp_events.sender = sink
    with TestClient(app, base_url="https://terminal.example") as client:
        number_a, slot_a, access_a, _ = issue(client, app, "agent-a")
        number_b, _, access_b, coord_b = issue(client, app, "agent-b")
        first = register(client, number_a)["result"]["id"]
        second = register(
            client, number_a, token="test-bearer-b", url=URL + "/second",
            evidence=access_b,
        )["result"]["id"]
        third = register(client, number_b)["result"]["id"]
        assert len({first, second, third}) == 3
        assert stored(app, first)["arguments_json"] == json.dumps(
            {"session_number": number_a}, separators=(",", ":")
        )
        assert stored(app, first)["recipient_role"] == "session_number"
        assert stored(app, first)["recipient_agent_id"] == slot_a["logical_agent_id"]
        assert register(client, number_a)["result"]["id"] == first
        tool(
            client, "coordinator", "message",
            {"action": "send", "scope": "local", "mode": "notify",
             "target": app.state.access_mesh.store.observed_identity(
                 app.state.access_mesh.store.slot(slot_a["issuer_id"], slot_a["slot_id"])
             )["public_name"], "text": "only A gets this"},
            evidence=coord_b,
        )
        client.portal.call(app.state.mcp_events.store.pump)
        client.portal.call(app.state.mcp_events.deliver_due)
        events = [(url, data) for url, data, _ in sink.requests
                  if data.get("name") == "message.received"]
        assert {url for url, _ in events} == {URL, URL + "/second"}
        assert len(events) == 2
        assert all(data["data"]["text"] == "only A gets this" for _, data in events)
        assert len({data["eventId"] for _, data in events}) == 1
        # Normal end/start retains the exact recipient and refresh ID.
        tool(client, "access", "session", {"action": "end"}, evidence=access_a)
        assert client.portal.call(
            app.state.access_events.message_resolver.still_bound, stored(app, first)
        )
        assert register(client, number_a)["error"]["code"] == -32001
        resumed = tool(
            client, "access", "session", {"action": "start"}, evidence=access_a
        )["session_number"]
        assert resumed == number_a
        assert register(client, number_a)["result"]["id"] == first
        assert remove(client, number_a)["result"] == {}
        assert stored(app, first) is None
        assert stored(app, second) is not None  # other authenticated callback owner
        assert stored(app, third) is not None  # same owner/callback, different number
        assert remove(client, number_a)["result"] == {}


def test_expiry_rejects_new_subscription_and_reuse_never_retargets(tmp_path):
    app = create_app(config(tmp_path))
    sink = Sink()
    app.state.access_events.sender = sink
    with TestClient(app, base_url="https://terminal.example") as client:
        number_a, slot_a, _, _ = issue(client, app, "old")
        sub_id = register(client, number_a)["result"]["id"]
        old_binding = stored(app, sub_id)
        clock = app.state.access_mesh.clock
        app.state.access_mesh.clock = lambda: datetime.now(UTC) + timedelta(days=1)
        assert register(client, number_a)["error"]["code"] == -32001
        app.state.access_mesh.clock = clock
        _, slot_b, _, _ = issue(client, app, "new")
        # Isolated fixture simulates the existing allocator's nonoverlapping reuse.
        now = datetime.now(UTC)
        with sqlite3.connect(app.state.settings.database_path) as db:
            db.execute(
                "UPDATE access_mesh_number_claims SET started_at=?,hard_expires_at=? "
                "WHERE issuer_id=? AND slot_id=?",
                ((now - timedelta(hours=2)).isoformat(),
                 (now - timedelta(hours=1)).isoformat(),
                 slot_a["issuer_id"], slot_a["slot_id"]),
            )
            db.execute(
                "UPDATE access_mesh_number_claims SET number=?,started_at=?,hard_expires_at=? "
                "WHERE issuer_id=? AND slot_id=?",
                (number_a, now.isoformat(), (now + timedelta(hours=1)).isoformat(),
                 slot_b["issuer_id"], slot_b["slot_id"]),
            )
            db.execute(
                "UPDATE access_mesh_slot_replicas SET state='deleted' "
                "WHERE issuer_id=? AND slot_id=?",
                (slot_a["issuer_id"], slot_a["slot_id"]),
            )
            db.execute(
                "UPDATE access_mesh_slot_replicas SET code_tag=? WHERE issuer_id=? AND slot_id=?",
                (app.state.access_mesh.store.code_tag(slot_b["issuer_id"], number_a),
                 slot_b["issuer_id"], slot_b["slot_id"]),
            )
        assert not client.portal.call(
            app.state.access_events.message_resolver.still_bound, old_binding
        )
        # A pending old delivery cannot erase the association and let a native
        # automatic refresh silently select the recycled recipient.
        with sqlite3.connect(app.state.settings.database_path) as db:
            stamp = utc_text()
            db.execute(
                "INSERT INTO mcp_event_records(event_id,source_key,name,occurred_at,"
                "data_json,recipient_agent_id) VALUES(?,?,?,?,?,?)",
                ("fixture-reuse", "fixture-reuse", "message.received", stamp,
                 json.dumps({"server": "firstbyte", "sender": "fixture",
                             "recipient": "fixture", "message_id": "fixture",
                             "text": "old recipient only", "timestamp": stamp}),
                 slot_a["logical_agent_id"]),
            )
            db.execute(
                "INSERT INTO mcp_event_deliveries(event_id,subscription_id,next_attempt_at) "
                "VALUES(?,?,?)", ("fixture-reuse", sub_id, stamp),
            )
        client.portal.call(app.state.access_events.deliver_due)
        assert stored(app, sub_id) is not None
        assert not any(data.get("name") == "message.received" for _, data, _ in sink.requests)
        with sqlite3.connect(app.state.settings.database_path) as db:
            assert db.execute(
                "SELECT state FROM mcp_event_deliveries WHERE event_id='fixture-reuse'"
            ).fetchone()[0] == "dead"
        changed = register(client, number_a)
        assert changed["error"]["code"] == -32001
        assert changed["error"]["data"]["reason"] == "recipient_binding_changed"
        assert "recipient changed" in changed["error"]["message"]
        assert stored(app, sub_id)["recipient_agent_id"] == slot_a["logical_agent_id"]
        assert remove(client, number_a)["result"] == {}
        new_id = register(client, number_a)["result"]["id"]
        assert new_id != sub_id
        assert stored(app, new_id)["recipient_agent_id"] == slot_b["logical_agent_id"]


def test_number_resolver_requires_read_scope_and_access_role(tmp_path):
    app = create_app(config(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        number, _, _, _ = issue(client, app, "scope")
        resolver = app.state.access_events.message_resolver
        context = VerifiedAdmissionContext(
            principal_id="different-authorized-agent", credential_id="fixture",
            scopes=frozenset({"terminal:execute"}), auth_generation=1,
            transport="mcp", auth_mode="bearer",
        )
        assert client.portal.call(resolver.resolve_number, context, number, "access") is None
        context = VerifiedAdmissionContext(
            principal_id="different-authorized-agent", credential_id="fixture",
            scopes=frozenset({"terminal:read"}), auth_generation=1,
            transport="mcp", auth_mode="bearer",
        )
        assert client.portal.call(resolver.resolve_number, context, number, "coordinator") is None
        assert client.portal.call(resolver.resolve_number, context, number, "access") is not None


def test_expiry_during_callback_challenge_cannot_commit_subscription(tmp_path):
    app = create_app(config(tmp_path))
    sink = Sink()

    async def expires_during_challenge(url, raw_body, headers):
        app.state.access_mesh.clock = lambda: datetime.now(UTC) + timedelta(days=1)
        return await sink(url, raw_body, headers)

    with TestClient(app, base_url="https://terminal.example") as client:
        number, _, _, _ = issue(client, app, "callback-race")
        app.state.access_events.sender = expires_during_challenge
        result = register(client, number)
        assert result["error"]["code"] == -32001
        assert result["error"]["message"] == "Subscription authorization expired"
        with sqlite3.connect(app.state.settings.database_path) as db:
            assert db.execute("SELECT COUNT(*) FROM mcp_event_subscriptions").fetchone()[0] == 0
        assert len(sink.requests) == 1


def test_generated_events_catalog_matches_native_runtime():
    artifact = Path(__file__).resolve().parents[1] / "docs/contracts/mcp-events-list.json"
    assert json.loads(artifact.read_text()) == event_catalog()


def test_oauth_number_subscription_requires_no_provider_metadata(tmp_path):
    from test_mcp_events_qa_golf3_revocation import ISSUER, provision, settings

    app = create_app(settings(tmp_path))
    sink = Sink()
    app.state.access_events.sender = sink
    with TestClient(app, base_url=ISSUER) as client:
        _, token, _, slot = provision(client, app)
        number = app.state.access_mesh.store.numbers.number_for_slot(
            slot["issuer_id"], slot["slot_id"]
        )
        result = register(client, number, token=token)
        assert "result" in result, result
        assert stored(app, result["result"]["id"])["recipient_agent_id"] == slot["logical_agent_id"]
        assert len(sink.requests) == 1


@pytest.mark.parametrize("revocation", ["credential", "slot"])
def test_revoked_number_binding_or_credentials_stop_pending_delivery(tmp_path, revocation):
    app = create_app(config(tmp_path))
    sink = Sink()
    app.state.access_events.sender = sink
    app.state.mcp_events.sender = sink
    with TestClient(app, base_url="https://terminal.example") as client:
        number, slot, _, _ = issue(client, app, "recipient")
        _, _, _, sender = issue(client, app, "sender")
        sub_id = register(client, number)["result"]["id"]
        tool(
            client, "coordinator", "message",
            {"action": "send", "scope": "local", "mode": "notify",
             "target": app.state.access_mesh.store.observed_identity(
                 app.state.access_mesh.store.slot(slot["issuer_id"], slot["slot_id"])
             )["public_name"], "text": "pending fixture"},
            evidence=sender,
        )
        client.portal.call(app.state.mcp_events.store.pump)
        if revocation == "credential":
            app.state.settings.bearer_tokens = "test-bearer-b"
        else:
            with sqlite3.connect(app.state.settings.database_path) as db:
                db.execute(
                    "UPDATE access_mesh_slot_replicas SET state='suspended' "
                    "WHERE issuer_id=? AND slot_id=?",
                    (slot["issuer_id"], slot["slot_id"]),
                )
        client.portal.call(app.state.mcp_events.deliver_due)
        assert not any(data.get("name") == "message.received" for _, data, _ in sink.requests)
        assert stored(app, sub_id) is None
