"""Subscription-held message identity without extending work authority."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_mcp_events_message_binding import KEY, URL, config, rpc, tool
from test_mcp_events_session_number import Sink, issue, remove, stored

from terminal_mcp.app import create_app
from terminal_mcp.core.access_mesh_grants import AccessMeshError, AccessSlotEvent
from terminal_mcp.storage.access_mesh import AccessMeshStore


def subscribe(client, number, *, token="test-bearer-a", url=URL, ttl=None, omit=False):
    params = {
        "name": "message.received",
        "arguments": {"session_number": number},
        "delivery": {"mode": "webhook", "url": url, "secret": KEY},
    }
    if not omit:
        params["ttlMs"] = ttl
    return rpc(client, "access", "events/subscribe", params, token=token)


def pins(app):
    with sqlite3.connect(app.state.settings.database_path) as db:
        db.row_factory = sqlite3.Row
        return [
            dict(r)
            for r in db.execute("SELECT * FROM access_mesh_recipient_holds ORDER BY hold_id")
        ]


def setup_app(tmp_path):
    app = create_app(config(tmp_path))
    sink = Sink()
    app.state.access_events.sender = sink
    app.state.mcp_events.sender = sink
    return app, sink


def test_ended_recipient_send_write_fence_and_last_subscription_removal(tmp_path):
    app, sink = setup_app(tmp_path)
    with TestClient(app, base_url="https://terminal.example") as client:
        number, claim, access_a, coord_a = issue(client, app, "held-a")
        _, _, _, coord_b = issue(client, app, "sender-b")
        first = subscribe(client, number)["result"]
        second = subscribe(client, number, token="test-bearer-b", url=URL + "/other")["result"]
        assert first["refreshBefore"] is None
        name = app.state.access_mesh.store.human_name(
            app.state.access_mesh.store.slot(claim["issuer_id"], claim["slot_id"])
        )
        original_deadline = claim["hard_expires_at"]
        tool(client, "access", "session", {"action": "end"}, evidence=access_a)
        renewed = subscribe(client, number)["result"]
        assert renewed["id"] == first["id"] and renewed["refreshBefore"] is None
        recipients = tool(
            client,
            "coordinator",
            "message",
            {"action": "recipients", "scope": "local"},
            evidence=coord_b,
        )["recipients"]
        assert any(
            r["public_name"] == name and r["session_state"] == "inactive" for r in recipients
        )
        result = tool(
            client,
            "coordinator",
            "message",
            {"action": "send", "scope": "local", "target": name, "text": "after work"},
            evidence=coord_b,
        )
        assert result["ok"]
        client.portal.call(app.state.mcp_events.store.pump)
        client.portal.call(app.state.mcp_events.deliver_due)
        events = [data for _, data, _ in sink.requests if data.get("name") == "message.received"]
        assert len(events) == 2 and all(e["data"]["text"] == "after work" for e in events)
        denied = rpc(
            client,
            "coordinator",
            "tools/call",
            {
                "name": "message",
                "arguments": {
                    "action": "send",
                    "scope": "local",
                    "target": "broadcast",
                    "text": "blocked",
                },
            },
            evidence=coord_a,
        )
        assert (
            "error" in denied
            or denied["result"].get("isError")
            or denied["result"].get("structuredContent", {}).get("return_to_chat")
        )
        assert app.state.access_mesh.store.numbers.winner(number)["hard_expires_at"] == (
            original_deadline
        )
        # Deletion and code rotation are fenced in the same slot transaction.
        slot = app.state.access_mesh.store.slot(claim["issuer_id"], claim["slot_id"])
        for kind, tag in (
            ("SlotDeleted", None),
            ("AccessCodeRotated", app.state.access_mesh.store.code_tag(slot.issuer_id, "9999")),
        ):
            event = AccessSlotEvent(
                issuer_id=slot.issuer_id,
                slot_id=slot.slot_id,
                logical_agent_id=slot.logical_agent_id,
                revision=slot.revision + 1,
                event_id="held-" + kind,
                kind=kind,
                code_tag=tag,
            )
            with pytest.raises(AccessMeshError, match="access_mesh_subscription_hold"):
                app.state.access_mesh.store.apply_event(event, authenticated_peer_id=slot.issuer_id)
        future = datetime.now(UTC) + timedelta(days=1)
        assert not app.state.access_mesh.store.numbers.reserve(
            number=number, attempt_id="new-owner", now=future
        )["ok"]
        assert remove(client, number)["result"] == {}
        assert stored(app, first["id"]) is None and stored(app, second["id"]) is not None
        assert len([p for p in pins(app) if p["state"] == "held"]) == 1
        assert remove(client, number, token="test-bearer-b", url=URL + "/other")["result"] == {}
        assert not app.state.access_mesh.store.recipient_holds.local_recipients()
        recipients = tool(
            client,
            "coordinator",
            "message",
            {"action": "recipients", "scope": "local"},
            evidence=coord_b,
        )["recipients"]
        assert name not in {r["public_name"] for r in recipients}
        assert all(p["state"] == "released" for p in pins(app))


def test_lifecycle_subscriptions_do_not_pin_and_claimed_unsubscribe_cannot_dispatch(tmp_path):
    app, _ = setup_app(tmp_path)
    with TestClient(app, base_url="https://terminal.example") as client:
        for name in ("agent.attached", "agent.inactive", "session.ended"):
            response = rpc(
                client,
                "access",
                "events/subscribe",
                {
                    "name": name,
                    "arguments": {},
                    "ttlMs": None,
                    "delivery": {"mode": "webhook", "url": URL, "secret": KEY},
                },
            )
            assert response["result"]["refreshBefore"] is None
        assert pins(app) == []
        number, claim, _, _ = issue(client, app, "dispatch")
        sub = subscribe(client, number)["result"]["id"]
        with sqlite3.connect(app.state.settings.database_path) as db:
            stamp = datetime.now(UTC).isoformat()
            db.execute(
                "INSERT INTO mcp_event_records(event_id,source_key,name,occurred_at,data_json) "
                "VALUES('race','race','message.received',?,'{}')",
                (stamp,),
            )
            db.execute(
                "INSERT INTO mcp_event_deliveries(event_id,subscription_id,next_attempt_at) "
                "VALUES('race',?,?)",
                (sub, stamp),
            )
        claimed = next(
            row
            for row in client.portal.call(app.state.mcp_events.store.due_deliveries)
            if row["subscription_id"] == sub
        )
        assert client.portal.call(app.state.mcp_events.store.dispatch_is_current, claimed)
        slot = app.state.access_mesh.store.slot(claim["issuer_id"], claim["slot_id"])
        before = app.state.access_mesh.store.observed_identity(slot)
        remove(client, number)
        assert not client.portal.call(app.state.mcp_events.store.dispatch_is_current, claimed)
        after = app.state.access_mesh.store.observed_identity(slot)
        assert after["work_session_id"] == before["work_session_id"]
        assert after["hard_expires_at"] == before["hard_expires_at"]
        assert after["session_lifecycle"]["state"] in {"active", "warning", "draining"}
        assert not app.state.access_mesh.store.numbers.reserve(
            number=number, attempt_id="last-unsubscribe-live-check"
        )["ok"]


def test_pin_replica_tombstones_refcounts_known_collision_and_restart(tmp_path):
    from test_access_mesh_session_numbers import T0

    path = tmp_path / "pins.db"
    kwargs = dict(
        local_node_id="b",
        trusted_issuers=frozenset({"a", "b"}),
        proof_key=b"fixture-proof-key-32-bytes-long",
    )
    store = AccessMeshStore(path, **kwargs)
    original = dict(
        number="1234",
        issuer_id="a",
        slot_id="slot",
        logical_agent_id="agent",
        started_at=T0,
        hard_expires_at=T0 + timedelta(minutes=1),
    )
    store.numbers.register(**original)
    row = dict(
        source_node_id="a",
        hold_id="sub1",
        recipient_agent_id="agent",
        issuer_id="a",
        slot_id="slot",
        number="1234",
        state="held",
        revision=1,
    )
    store.recipient_holds.merge([row, {**row, "hold_id": "sub2"}], authenticated_peer_id="a")
    overlap = T0 + timedelta(seconds=30)
    with pytest.raises(AccessMeshError, match="access_mesh_subscription_hold"):
        store.numbers.register(
            **{
                **original,
                "issuer_id": "b",
                "slot_id": "overlap",
                "logical_agent_id": "new",
                "started_at": overlap,
                "hard_expires_at": overlap + timedelta(minutes=1),
            }
        )
    later = T0 + timedelta(days=1)
    assert not store.numbers.reserve(number="1234", attempt_id="reuse", now=later)["ok"]
    with pytest.raises(AccessMeshError, match="access_mesh_subscription_hold"):
        store.numbers.register(
            **{
                **original,
                "issuer_id": "b",
                "slot_id": "new",
                "logical_agent_id": "new",
                "started_at": later,
                "hard_expires_at": later + timedelta(minutes=1),
            }
        )
    store.recipient_holds.merge(
        [{**row, "state": "released", "revision": 2}], authenticated_peer_id="a"
    )
    assert not store.numbers.reserve(number="1234", attempt_id="reuse", now=later)["ok"]
    store = AccessMeshStore(path, **kwargs)
    store.recipient_holds.merge([row], authenticated_peer_id="a")  # delayed old packet
    assert not store.numbers.reserve(number="1234", attempt_id="reuse", now=later)["ok"]
    store.recipient_holds.merge(
        [{**row, "hold_id": "sub2", "state": "released", "revision": 2}],
        authenticated_peer_id="a",
    )
    assert store.numbers.reserve(number="1234", attempt_id="reuse", now=later)["ok"]
    with pytest.raises(AccessMeshError, match="invalid_recipient_hold_snapshot"):
        store.recipient_holds.merge([row], authenticated_peer_id="b")


def test_no_expiry_delivery_recovers_after_restart_without_resubscribe(tmp_path):
    app, _ = setup_app(tmp_path)
    with TestClient(app, base_url="https://terminal.example") as client:
        number, claim, access, _ = issue(client, app, "restart-target")
        sub_id = subscribe(client, number)["result"]["id"]
        binding = stored(app, sub_id)
        name = app.state.access_mesh.store.human_name(
            app.state.access_mesh.store.slot(claim["issuer_id"], claim["slot_id"])
        )
        tool(client, "access", "session", {"action": "end"}, evidence=access)
    restarted, sink = setup_app(tmp_path)
    with TestClient(restarted, base_url="https://terminal.example") as client:
        assert stored(restarted, sub_id) == binding
        _, _, _, sender = issue(client, restarted, "restart-sender")
        assert tool(
            client,
            "coordinator",
            "message",
            {
                "action": "send",
                "scope": "local",
                "target": name,
                "text": "restart recovered",
            },
            evidence=sender,
        )["ok"]
        client.portal.call(restarted.state.mcp_events.store.pump)
        client.portal.call(restarted.state.mcp_events.deliver_due)
        events = [data for _, data, _ in sink.requests if data.get("name") == "message.received"]
        assert len(events) == 1 and events[0]["data"]["text"] == "restart recovered"


@pytest.mark.asyncio
async def test_hold_http_anti_entropy_outage_and_withdrawal(tmp_path):
    import httpx
    from test_access_mesh_replication_http import Routes, actor, node

    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        source, source_rep = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        replica, replica_rep = await node(tmp_path, "bacloud", "firstbyte", routes, client)
        issued = await source.issue(actor(), code="7312")
        await source_rep.tick()
        row = {
            "source_node_id": "firstbyte",
            "hold_id": "fixture-sub",
            "recipient_agent_id": issued["logical_agent_id"],
            "issuer_id": "firstbyte",
            "slot_id": issued["slot_id"],
            "number": "7312",
            "state": "held",
            "revision": 1,
        }
        source.store.recipient_holds.merge([row], authenticated_peer_id="firstbyte")
        await replica_rep.sync_recipient_holds(replica_rep.peers[0])
        await replica_rep.sync_numbers(replica_rep.peers[0])
        expected = [
            {key: row[key] for key in ("recipient_agent_id", "issuer_id", "slot_id", "number")}
        ]
        assert replica.store.recipient_holds.local_recipients() == expected
        attached = await replica.attach(actor(), issuer_node_id="firstbyte", access_code="7312")
        assert attached["logical_agent_id"] == issued["logical_agent_id"]
        from test_access_mesh_replication_http import T0

        replica.clock = lambda: T0 + timedelta(days=1)
        held = replica.store.recipient_holds.local_recipients()
        assert len(held) == 1 and held[0]["recipient_agent_id"] == issued["logical_agent_id"]
        # An issuer-owned hold confers presence independently of attachments.
        with replica.store._connect() as db:
            db.execute("DELETE FROM access_mesh_attachments")
        assert replica.store.recipient_holds.local_recipients() == expected
        routes.offline.add("firstbyte")
        future = datetime.now(UTC) + timedelta(days=1)
        assert not replica.store.numbers.reserve(
            number="7312", attempt_id="outage-reuse", now=future
        )["ok"]
        source.store.recipient_holds.merge(
            [{**row, "state": "released", "revision": 2}],
            authenticated_peer_id="firstbyte",
        )
        routes.offline.clear()
        await replica_rep.sync_recipient_holds(replica_rep.peers[0])
        with replica.store._connect() as db:
            state = db.execute(
                "SELECT state,revision FROM access_mesh_recipient_holds "
                "WHERE source_node_id='firstbyte' AND hold_id='fixture-sub'"
            ).fetchone()
        assert tuple(state) == ("released", 2)
        # After withdrawal, the remaining replicated Slot/grant alone is ineligible.
        assert replica.store.slot("firstbyte", issued["slot_id"]).state == "active"
        assert replica.store.recipient_holds.local_recipients() == []


def test_concurrent_refresh_and_unsubscribe_leave_pin_transactionally_consistent(tmp_path):
    import asyncio

    from terminal_mcp.mcp.message_binding import MessageSubscriber

    app, _ = setup_app(tmp_path)
    with TestClient(app, base_url="https://terminal.example") as client:
        number, _, _, _ = issue(client, app, "race-pin")
        sub_id = subscribe(client, number)["result"]["id"]
        record = stored(app, sub_id)
        binding = MessageSubscriber(
            recipient_agent_id=record["recipient_agent_id"],
            work_session_id=record["recipient_session_id"],
            session_epoch=record["recipient_epoch"],
            connection_key=record["recipient_connection_key"],
            issuer_id=record["recipient_issuer_id"],
            slot_id=record["recipient_slot_id"],
            role=record["recipient_role"],
        )

        async def race():
            store = app.state.mcp_events.store
            await asyncio.gather(
                store.unsubscribe(sub_id, record["principal_id"]),
                store.save_subscription(
                    sub_id=sub_id,
                    principal=record["principal_id"],
                    credential_id=record["credential_id"],
                    auth_mode=record["auth_mode"],
                    name="message.received",
                    callback=URL,
                    secret=KEY,
                    no_expiry=True,
                    message_binding=binding,
                    refresh_existing=True,
                    authorization_checker=app.state.access_events.access_checker,
                    arguments={"session_number": number},
                ),
            )

        client.portal.call(race)
        exists = stored(app, sub_id) is not None
        assert (pins(app)[0]["state"] == "held") is exists
