"""TMCP-04 independent synthetic-only Access Events acceptance regressions."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from test_mcp_events_qa_golf3_revocation import (
    ISSUER,
    SECRET,
    URL,
    CallbackSink,
    provision,
    settings,
    subscribe,
)

from terminal_mcp.app import create_app
from terminal_mcp.core.orchestration import utc_text


def rows(app):
    """Subscription, immutable binding and verification cache counts."""
    with sqlite3.connect(app.state.settings.database_path) as db:
        return tuple(
            db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "mcp_event_subscriptions",
                "mcp_event_message_bindings",
                "mcp_event_verified_callbacks",
            )
        )


def test_active_slot_reassigned_during_challenge_is_rejected_atomically(tmp_path):
    """State alone is insufficient: exact immutable agent must still match."""
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url=ISSUER) as client:
        _, token, metadata, slot = provision(client, app)

        class ReassignSlot(CallbackSink):
            async def __call__(self, url, body, headers):
                if json.loads(body).get("type") == "verification":
                    with sqlite3.connect(app.state.settings.database_path) as db:
                        changed = db.execute(
                            "UPDATE access_mesh_slot_replicas "
                            "SET logical_agent_id=? WHERE issuer_id=? AND slot_id=?",
                            ("synthetic-other-immutable-agent", slot["issuer_id"], slot["slot_id"]),
                        )
                        assert changed.rowcount == 1
                return await super().__call__(url, body, headers)

        sink = ReassignSlot()
        app.state.access_events.sender = sink
        response = subscribe(client, token, metadata)
        assert response.get("error", {}).get("code") == -32001, response
        assert len(sink.calls) == 1 and sink.calls[0]["type"] == "verification"
        with sqlite3.connect(app.state.settings.database_path) as db:
            state = db.execute(
                "SELECT state,logical_agent_id FROM access_mesh_slot_replicas "
                "WHERE issuer_id=? AND slot_id=?",
                (slot["issuer_id"], slot["slot_id"]),
            ).fetchone()
        assert state == ("active", "synthetic-other-immutable-agent")
        assert rows(app) == (0, 0, 0)


def test_callback_denial_leaves_no_registration_or_verification_cache(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url=ISSUER) as client:
        _, token, metadata, _ = provision(client, app)

        class DenyChallenge(CallbackSink):
            async def __call__(self, url, body, headers):
                assert json.loads(body).get("type") == "verification"
                self.calls.append(json.loads(body))
                return 403, b"synthetic-denied"

        sink = DenyChallenge()
        app.state.access_events.sender = sink
        response = subscribe(client, token, metadata)
        assert response.get("error", {}).get("code") == -32015, response
        assert len(sink.calls) == 1
        assert rows(app) == (0, 0, 0)


def test_midwrite_binding_failure_rolls_back_and_retry_commits(tmp_path):
    """Abort after subscription INSERT, before callback cache: no partial writes."""
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url=ISSUER, raise_server_exceptions=False) as client:
        _, token, metadata, _ = provision(client, app)
        sink = CallbackSink()
        app.state.access_events.sender = sink
        with sqlite3.connect(app.state.settings.database_path) as db:
            db.execute(
                "CREATE TRIGGER qa_bravo3_abort_binding "
                "BEFORE INSERT ON mcp_event_message_bindings "
                "BEGIN SELECT RAISE(ABORT, 'synthetic qa binding failure'); END"
            )
        response = client.post(
            "/terminal-mcp/access/v1/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 991,
                "method": "events/subscribe",
                "params": {
                    "name": "message.received",
                    "arguments": {},
                    "delivery": {"mode": "webhook", "url": URL, "secret": SECRET},
                    "_meta": metadata,
                },
            },
            headers={
                "Authorization": "Bearer " + token,
                "accept": "application/json, text/event-stream",
            },
        )
        assert response.status_code >= 500, response.text
        assert rows(app) == (0, 0, 0)
        with sqlite3.connect(app.state.settings.database_path) as db:
            db.execute("DROP TRIGGER qa_bravo3_abort_binding")
        recovered = subscribe(client, token, metadata)
        assert "result" in recovered, recovered
        assert rows(app) == (1, 1, 1)
        assert len(sink.calls) == 2


def test_only_local_message_events_expire_at_24h_boundary(tmp_path):
    """Old private event + its delivery are purged; newer and attach retained."""
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url=ISSUER) as client:
        now = datetime.now(UTC)
        older = utc_text(now - timedelta(hours=24, seconds=2))
        newer = utc_text(now - timedelta(hours=23))
        with sqlite3.connect(app.state.settings.database_path) as db:
            for event_id, name, timestamp in (
                ("bravo3-expired-private", "message.received", older),
                ("bravo3-recent-private", "message.received", newer),
                ("bravo3-old-attach", "agent.attached", older),
            ):
                db.execute(
                    "INSERT INTO mcp_event_records("
                    "event_id,source_key,name,occurred_at,data_json,recipient_agent_id,namespace) "
                    "VALUES(?,?,?,?,?,?,NULL)",
                    (event_id, "qa-" + event_id, name, timestamp, "{}", "qa-agent"),
                )
                db.execute(
                    "INSERT INTO mcp_event_deliveries("
                    "event_id,subscription_id,next_attempt_at) VALUES(?,?,?)",
                    (event_id, "synthetic-qa-subscriber", newer),
                )
        client.portal.call(app.state.mcp_events.store.pump)
        with sqlite3.connect(app.state.settings.database_path) as db:
            remaining_events = {
                x[0]
                for x in db.execute(
                    "SELECT event_id FROM mcp_event_records WHERE event_id LIKE 'bravo3-%'"
                )
            }
            remaining_deliveries = {
                x[0]
                for x in db.execute(
                    "SELECT event_id FROM mcp_event_deliveries WHERE event_id LIKE 'bravo3-%'"
                )
            }
        expected = {"bravo3-recent-private", "bravo3-old-attach"}
        assert remaining_events == expected
        assert remaining_deliveries == expected


def test_oauth_revocation_writer_cannot_interleave_and_later_delivery_denied(tmp_path):
    """Credential revoke is serialized with final check; later revoke blocks send."""
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url=ISSUER) as client:
        client_id, token, metadata, slot = provision(client, app)
        sink = CallbackSink()
        app.state.access_events.sender = sink
        original = app.state.access_events.access_checker
        probes = []

        async def guarded_checker(item):
            allowed = await original(item)
            if allowed and item.get("name") == "message.received":
                probes.append("checked")
                # A competing OAuth credential writer must not commit while
                # the store is between its final checker and the INSERT.
                try:
                    with sqlite3.connect(app.state.settings.database_path, timeout=0) as rival:
                        rival.execute("DELETE FROM oauth_clients WHERE client_id=?", (client_id,))
                except sqlite3.OperationalError as exc:
                    assert "locked" in str(exc).lower()
                else:
                    raise AssertionError("OAuth delete interleaved final validity and commit")
            return allowed

        app.state.access_events.access_checker = guarded_checker
        saved = subscribe(client, token, metadata)
        assert "result" in saved, saved
        sid = saved["result"]["id"]
        assert probes == ["checked"]
        assert rows(app) == (1, 1, 1)

        with sqlite3.connect(app.state.settings.database_path) as db:
            removed = db.execute("DELETE FROM oauth_clients WHERE client_id=?", (client_id,))
            assert removed.rowcount == 1
            stamp = utc_text()
            db.execute(
                "INSERT INTO mcp_event_records("
                "event_id,source_key,name,occurred_at,data_json,recipient_agent_id,namespace) "
                "VALUES(?,?,?,?,?,?,NULL)",
                (
                    "bravo3-oauth-revoked-event",
                    "qa-bravo3-oauth-revoked-event",
                    "message.received",
                    stamp,
                    json.dumps({"server": "secondary-qa", "text": "synthetic only"}),
                    slot["logical_agent_id"],
                ),
            )
            db.execute(
                "INSERT INTO mcp_event_deliveries(event_id,subscription_id,next_attempt_at) "
                "VALUES(?,?,?)",
                ("bravo3-oauth-revoked-event", sid, stamp),
            )
        client.portal.call(app.state.access_events.deliver_due)
        assert len(sink.calls) == 1 and sink.calls[0]["type"] == "verification"
        with sqlite3.connect(app.state.settings.database_path) as db:
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM mcp_event_subscriptions WHERE id=?", (sid,)
                ).fetchone()[0]
                == 0
            )
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM mcp_event_message_bindings WHERE subscription_id=?",
                    (sid,),
                ).fetchone()[0]
                == 0
            )
