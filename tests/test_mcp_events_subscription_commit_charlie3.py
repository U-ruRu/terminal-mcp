"""Disposable regressions for the final Access Events subscription commit gate."""

from __future__ import annotations

import json
import sqlite3

from fastapi.testclient import TestClient
from test_mcp_events_qa_golf3_revocation import (
    ISSUER,
    CallbackSink,
    provision,
    settings,
    subscribe,
)

from terminal_mcp.app import create_app


def subscription_counts(app):
    with sqlite3.connect(app.state.settings.database_path) as db:
        return tuple(
            db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "mcp_event_subscriptions",
                "mcp_event_message_bindings",
                "mcp_event_verified_callbacks",
            )
        )


def test_regular_authorized_registration_commits_exact_binding(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url=ISSUER) as client:
        client_id, token, pair, slot = provision(client, app)
        app.state.access_events.sender = CallbackSink()

        result = subscribe(client, token, pair)
        assert "result" in result, result
        sub_id = result["result"]["id"]
        with sqlite3.connect(app.state.settings.database_path) as db:
            row = db.execute(
                "SELECT s.credential_id,s.auth_mode,b.recipient_agent_id,"
                "b.recipient_connection_key,b.recipient_issuer_id,"
                "b.recipient_slot_id,b.recipient_role "
                "FROM mcp_event_subscriptions s "
                "JOIN mcp_event_message_bindings b ON b.subscription_id=s.id "
                "WHERE s.id=?",
                (sub_id,),
            ).fetchone()
        assert row is not None
        assert row[0:3] == ("oauth:" + client_id, "oauth", slot["logical_agent_id"])
        assert row[3] and row[4:] == (slot["issuer_id"], slot["slot_id"], "access")
        assert subscription_counts(app) == (1, 1, 1)


def test_oauth_client_revoked_during_callback_fails_without_rows(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url=ISSUER) as client:
        client_id, token, pair, _ = provision(client, app)

        class RevokeCredential(CallbackSink):
            async def __call__(self, url, body, headers):
                if json.loads(body).get("type") == "verification":
                    await app.state.oauth_store.delete_client(client_id)
                return await super().__call__(url, body, headers)

        app.state.access_events.sender = RevokeCredential()
        result = subscribe(client, token, pair)
        assert result.get("error", {}).get("code") == -32001, result
        assert subscription_counts(app) == (0, 0, 0)


def test_checker_failure_rolls_back_entire_subscription(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url=ISSUER) as client:
        _, token, pair, _ = provision(client, app)
        app.state.access_events.sender = CallbackSink()

        async def unavailable_checker(_item):
            raise RuntimeError("simulated read-only authorization failure")

        app.state.access_events.access_checker = unavailable_checker
        result = subscribe(client, token, pair)
        assert result.get("error", {}).get("code") == -32001, result
        assert subscription_counts(app) == (0, 0, 0)


def test_revocation_writer_cannot_cross_final_check_and_commit(tmp_path):
    """Final credential/binding reads and the insert share a writer lock."""
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url=ISSUER) as client:
        _, token, pair, slot = provision(client, app)
        app.state.access_events.sender = CallbackSink()
        original_checker = app.state.access_events.access_checker
        lock_probes = []

        async def final_checker(item):
            allowed = await original_checker(item)
            if allowed and item.get("name") == "message.received":
                lock_probes.append(True)
                # Deterministic independent SQLite writer attempt. Without
                # BEGIN IMMEDIATE before validation, this revokes the slot.
                try:
                    with sqlite3.connect(app.state.settings.database_path, timeout=0) as rival:
                        rival.execute(
                            "UPDATE access_mesh_slot_replicas SET state='suspended' "
                            "WHERE issuer_id=? AND slot_id=?",
                            (slot["issuer_id"], slot["slot_id"]),
                        )
                except sqlite3.OperationalError as exc:
                    assert "locked" in str(exc).lower()
                else:
                    raise AssertionError("revocation committed between check and insert")
            return allowed

        app.state.access_events.access_checker = final_checker
        result = subscribe(client, token, pair)
        assert "result" in result, result
        assert lock_probes == [True]
        assert subscription_counts(app) == (1, 1, 1)

        # Once the transaction commits a subsequent authorized revocation can
        # take the writer lock and must make delivery reject the subscription.
        with sqlite3.connect(app.state.settings.database_path) as db:
            changed = db.execute(
                "UPDATE access_mesh_slot_replicas SET state='suspended' "
                "WHERE issuer_id=? AND slot_id=?",
                (slot["issuer_id"], slot["slot_id"]),
            )
            assert changed.rowcount == 1
            state = db.execute(
                "SELECT state FROM access_mesh_slot_replicas WHERE issuer_id=? AND slot_id=?",
                (slot["issuer_id"], slot["slot_id"]),
            ).fetchone()
        assert state == ("suspended",)
