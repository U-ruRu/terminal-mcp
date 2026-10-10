"""Isolated local-message cutover regression; no live servers or databases."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_access_mesh_messages_v2 import _public_name, actor
from test_access_mesh_messages_v2 import mesh_pair as mesh_pair_fixture

from terminal_mcp.application.access_mesh_messages import AccessMeshMessaging
from terminal_mcp.storage.access_mesh_messages import (
    CUTOVER_REASON,
    AccessMeshMessageStore,
    MeshMessagingError,
)

mesh_pair = mesh_pair_fixture


@pytest.mark.asyncio
async def test_local_cycle_ack_replies_and_remote_recipient_exclusion(mesh_pair):
    local, other = mesh_pair.nodes["firstbyte"], mesh_pair.nodes["bacloud"]
    recipient = _public_name(local.mesh.store.slots["carol"])
    remote_only = _public_name(local.mesh.store.slots["bob"])
    recipients = await local.message(actor("alice"), recipients=True)
    assert recipients["ok"] and {r["public_name"] for r in recipients["recipients"]} == {recipient}
    missing = await local.message(actor("alice"), text="remote forbidden", target=remote_only)
    assert missing["code"] == "recipient_not_found"
    assert not local.store.local_cutover_report()["terminate_jobs"]
    sent = await local.message(
        actor("alice"), text="please acknowledge", target=recipient, mode="ack"
    )
    assert sent["ok"] and sent["scope"] == "local" and sent["delivered_to"] == [recipient]
    message_hash = sent["message_hash"]
    # Existing messages addressed to a different recipient remain forbidden.
    with pytest.raises(MeshMessagingError, match="message_forbidden"):
        local.store.acknowledge(message_hash, "bob", local.mesh.now.isoformat())
    inbox = await local.message(actor("carol"), detail="full")
    assert any(m["message_hash"] == message_hash for m in inbox["messages"])
    first = await local.message(actor("carol"), message_hash=message_hash)
    second = await local.message(actor("carol"), message_hash=message_hash)
    assert first["ok"] and second["ok"]
    with local.store.connect() as db:
        row = db.execute(
            "SELECT read_at FROM coordination_message_recipients WHERE message_hash=?",
            (message_hash,),
        ).fetchone()
        assert row[0] is not None
    reply1 = await local.message(actor("carol"), message_hash=message_hash, text="first")
    reply2 = await local.message(actor("carol"), message_hash=message_hash, text="second")
    assert reply1["ok"] and reply2["ok"] and reply1["message_hash"] != reply2["message_hash"]
    assert local.store.wire(reply1["message_hash"])["reply_to"] == message_hash
    assert local.store.wire(reply2["message_hash"])["reply_to"] == message_hash
    history = await local.message(actor("alice"), history=True, detail="full")
    assert {reply1["message_hash"], reply2["message_hash"]}.issubset(
        {m["message_hash"] for m in history["messages"]}
    )
    assert not mesh_pair.calls
    assert other.store.local_cutover_report()["retained"] == 0


@pytest.mark.asyncio
async def test_cutover_pending_jobs_expiry_and_restart(mesh_pair):
    local = mesh_pair.nodes["firstbyte"]
    sent = await local.message(actor("alice"), text="cutover fixture", scope="local")
    assert sent["ok"]
    message_hash = sent["message_hash"]
    wire = local.store.wire(message_hash)
    with local.store.connect() as db:
        local.store.queue(db, "bacloud", "deliver", {"message": wire, "excluded": []})
    before = local.store.local_cutover_report()
    assert before["retained"] == 1 and before["terminate_jobs"]["deliver"] == 1
    first_cutover = local.store.activate_local_cutover()
    second_cutover = local.store.activate_local_cutover()  # idempotent
    assert first_cutover["processed_jobs"] == 1
    assert second_cutover["processed_jobs"] == 0
    with local.store.connect() as db:
        row = db.execute(
            "SELECT attempts,acked_at,terminal_reason FROM access_mesh_message_outbox"
        ).fetchone()
    assert tuple(row) == (0, None, CUTOVER_REASON)
    assert local.store.pending() == []
    old_time = local.mesh.now + timedelta(hours=24, seconds=1)
    local.mesh.now = old_time
    assert local.store.wire(message_hash) is None
    assert local.store.page("carol", history=True) == []
    with pytest.raises(MeshMessagingError, match="message_not_found"):
        local.store.acknowledge(message_hash, "carol", old_time.isoformat())
    assert local.store.prune_expired() == 1
    # After the physical sweep, the deleted message must remain not_found,
    # not message_forbidden just because its recipient row is also gone.
    with pytest.raises(MeshMessagingError, match="message_not_found"):
        local.store.acknowledge(message_hash, "carol", old_time.isoformat())
    with local.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM access_mesh_messages").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM access_mesh_message_outbox").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["notify", "ack", "alert"])
async def test_all_local_modes_support_inbox_idempotent_ack_reply_and_history(mesh_pair, mode):
    local = mesh_pair.nodes["firstbyte"]
    recipient = _public_name(local.mesh.store.slots["carol"])

    sent = await local.message(
        actor("alice", request_id=f"send-{mode}"),
        text=f"{mode} message",
        target=recipient,
        mode=mode,
    )
    assert sent["ok"] and sent["scope"] == "local"
    message_hash = sent["message_hash"]

    inbox = await local.message(actor("carol"), detail="full")
    assert message_hash in {item["message_hash"] for item in inbox["messages"]}

    ack1 = await local.message(actor("carol"), message_hash=message_hash)
    ack2 = await local.message(actor("carol"), message_hash=message_hash)
    assert ack1["ok"] and ack2["ok"]

    reply = await local.message(
        actor("carol", request_id=f"reply-{mode}"),
        message_hash=message_hash,
        text=f"{mode} reply",
    )
    assert reply["ok"] and reply["reply_to"] == message_hash and reply["scope"] == "local"

    history = await local.message(actor("alice"), history=True, detail="full")
    hashes = {item["message_hash"] for item in history["messages"]}
    assert {message_hash, reply["message_hash"]}.issubset(hashes)
    assert not mesh_pair.calls
    assert local.store.pending() == []


@pytest.mark.asyncio
async def test_legacy_fleet_scope_is_explicitly_disabled_without_mutation(mesh_pair):
    local = mesh_pair.nodes["firstbyte"]
    recipient = _public_name(local.mesh.store.slots["carol"])

    result = await local.message(
        actor("alice"), text="must not leave node", target=recipient, scope="fleet"
    )
    assert result["ok"] is False and result["code"] == "feature_disabled"
    recipients = await local.message(actor("alice"), recipients=True, scope="fleet")
    assert recipients["ok"] is False and recipients["code"] == "feature_disabled"

    with local.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM access_mesh_messages").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM access_mesh_message_outbox").fetchone()[0] == 0
    assert not mesh_pair.calls


@pytest.mark.asyncio
async def test_same_agent_attached_on_two_nodes_has_independent_local_histories(mesh_pair):
    firstbyte = mesh_pair.nodes["firstbyte"]
    bacloud = mesh_pair.nodes["bacloud"]
    carol_name = _public_name(firstbyte.mesh.store.slots["carol"])

    first = await firstbyte.message(
        actor("alice", request_id="firstbyte-send"),
        text="firstbyte local",
        target=carol_name,
    )
    second = await bacloud.message(
        actor("bob", request_id="bacloud-send"),
        text="bacloud local",
        target=carol_name,
    )
    assert first["ok"] and second["ok"] and first["message_hash"] != second["message_hash"]

    fb_history = await firstbyte.message(actor("carol"), history=True, detail="full")
    bac_history = await bacloud.message(actor("carol"), history=True, detail="full")
    assert {m["message_hash"] for m in fb_history["messages"]} == {first["message_hash"]}
    assert {m["message_hash"] for m in bac_history["messages"]} == {second["message_hash"]}
    assert not mesh_pair.calls


@pytest.mark.asyncio
async def test_ttl_boundary_ack_does_not_extend_reply_has_own_ttl_and_restart_prunes(mesh_pair):
    local = mesh_pair.nodes["firstbyte"]
    created = local.mesh.now
    for slot in local.mesh.store.slots.values():
        slot.expires_at = created + timedelta(days=3)

    recipient = _public_name(local.mesh.store.slots["carol"])
    parent = await local.message(
        actor("alice", request_id="ttl-parent"),
        text="parent",
        target=recipient,
        mode="ack",
    )
    blocker = await local.message(
        actor("alice", request_id="ttl-blocker"),
        text="block until expiry",
        target=recipient,
        mode="alert",
    )
    assert {x["message_hash"] for x in await local.agent_store.message_obligations("carol")} >= {
        blocker["message_hash"]
    }

    local.mesh.now = created + timedelta(hours=23)
    assert (await local.message(actor("carol"), message_hash=parent["message_hash"]))["ok"]
    reply = await local.message(
        actor("carol", request_id="ttl-reply"),
        message_hash=parent["message_hash"],
        text="late reply",
    )

    local.mesh.now = created + timedelta(hours=24)
    assert local.store.wire(parent["message_hash"]) is None
    assert local.store.wire(blocker["message_hash"]) is None
    assert local.store.wire(reply["message_hash"]) is not None
    expired_ack = await local.message(actor("carol"), message_hash=parent["message_hash"])
    assert expired_ack["code"] == "message_not_found"
    assert blocker["message_hash"] not in {
        x["message_hash"] for x in await local.agent_store.message_obligations("carol")
    }

    reopened = AccessMeshMessageStore(
        local.store.path, "firstbyte", now_provider=lambda: local.mesh.now
    )
    assert reopened.wire(parent["message_hash"]) is None
    with reopened.connect() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM access_mesh_messages WHERE message_hash IN (?,?)",
                (parent["message_hash"], blocker["message_hash"]),
            ).fetchone()[0]
            == 2
        )

    # A running service sweeps once per minute (well inside the five-minute
    # requirement); tick is that sweep without sleeping in the test.
    await local.tick()

    after_restart = AccessMeshMessageStore(
        local.store.path, "firstbyte", now_provider=lambda: local.mesh.now
    )
    assert after_restart.prune_expired() == 0
    with after_restart.connect() as db:
        remaining = {
            row[0] for row in db.execute("SELECT message_hash FROM access_mesh_messages").fetchall()
        }
    assert reply["message_hash"] in remaining
    assert parent["message_hash"] not in remaining
    assert blocker["message_hash"] not in remaining


@pytest.mark.asyncio
async def test_old_invalid_message_is_ignored_and_outbox_finalizes_without_ack(mesh_pair):
    """Malformed old wire/receipt metadata must not obstruct the new protocol."""
    local = mesh_pair.nodes["firstbyte"]
    legacy_id = "bacloud:meshmsg:legacy-inbound"
    with local.store.connect() as db:
        db.execute(
            "INSERT INTO coordination_messages(message_hash,sender_agent_id,target_name,text,"
            "created_at,require_reply,alert,delivery_mode) VALUES(?,?,?,?,?,?,?,?)",
            (legacy_id, "alice", "broadcast", "old confidential data", "bad-time", 1, 1, "alert"),
        )
        db.execute(
            "INSERT INTO coordination_message_recipients(message_hash,recipient_agent_id) "
            "VALUES(?,?)",
            (legacy_id, "carol"),
        )
        db.execute(
            "INSERT INTO access_mesh_messages(message_hash,wire_json,origin_node_id,receipt_json) "
            "VALUES(?,?,?,?)",
            (legacy_id, '{"created_at":"ambiguous"}', "bacloud", "{}"),
        )
        db.execute(
            "INSERT INTO access_mesh_message_outbox(peer_id,kind,event_key,payload_json,"
            "message_hash,attempts,acked_at) VALUES(?,?,?,?,?,?,NULL)",
            ("bacloud", "receipt", "legacy-receipt-1", "{}", legacy_id, 91),
        )

    assert local.store.wire(legacy_id) is None
    assert await local.agent_store.message_obligations("carol") == []
    assert not (await local.message(actor("carol"), history=True, detail="full"))["messages"]
    assert (await local.message(actor("carol"), message_hash=legacy_id))[
        "code"
    ] == "message_not_found"
    assert (await local.message(actor("carol"), message_hash=legacy_id, text="reply"))[
        "code"
    ] == "message_not_found"
    report = local.store.local_cutover_report()
    assert report["blockers"] == [] and report["retained"] == 0
    first = local.store.activate_local_cutover()
    second = local.store.activate_local_cutover()
    assert first["processed_jobs"] == 1 and second["processed_jobs"] == 0
    with local.store.connect() as db:
        row = db.execute(
            "SELECT attempts,acked_at,terminal_reason FROM access_mesh_message_outbox"
        ).fetchone()
        assert tuple(row) == (91, None, CUTOVER_REASON)
        assert (
            db.execute(
                "SELECT COUNT(*) FROM coordination_messages WHERE message_hash=?", (legacy_id,)
            ).fetchone()[0]
            == 1
        )
    assert local.store.prune_expired() == 0  # cleanup never deletes old junk
    await local.start()
    await local.tick()
    await local.stop()
    with local.store.connect() as db:
        row = db.execute(
            "SELECT attempts,acked_at,terminal_reason FROM access_mesh_message_outbox"
        ).fetchone()
        assert tuple(row) == (91, None, CUTOVER_REASON)
    assert not mesh_pair.calls


@pytest.mark.asyncio
async def test_storage_rejects_new_peer_delivery_jobs_before_commit(mesh_pair):
    local = mesh_pair.nodes["firstbyte"]
    recipient = {
        "logical_agent_id": "carol",
        "public_name": _public_name(local.mesh.store.slots["carol"]),
    }
    wire = {
        "protocol": 1,
        "message_hash": "firstbyte:localmsg:peer-forwarding-must-stay-disabled",
        "origin_node_id": "firstbyte",
        "sender_id": "alice",
        "sender_name": _public_name(local.mesh.store.slots["alice"]),
        "target": recipient["public_name"],
        "text": "must not commit",
        "created_at": local.mesh.now.isoformat(),
        "mode": "notify",
        "require_reply": False,
        "alert": False,
        "namespace": None,
        "task_id": None,
        "scope": "local",
        "reply_to": None,
    }

    with pytest.raises(MeshMessagingError, match="feature_disabled"):
        local.store.accept(wire, [recipient], peers=["bacloud"])

    with local.store.connect() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM access_mesh_messages WHERE message_hash=?",
                (wire["message_hash"],),
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM access_mesh_message_outbox WHERE message_hash=?",
                (wire["message_hash"],),
            ).fetchone()[0]
            == 0
        )


@pytest.mark.asyncio
async def test_legacy_peer_message_request_is_terminally_rejected_without_queue(mesh_pair):
    local = mesh_pair.nodes["firstbyte"]
    result = await local.handle_peer("bacloud", "deliver", {"anything": "legacy"})
    assert result["ok"] is False
    assert result["code"] == "feature_disabled"
    assert result["outcome"] == "not_committed"
    old_discovery = await local.handle_peer("bacloud", "recipients", {"after": "", "limit": 100})
    assert old_discovery["code"] == "feature_disabled"

    presence = await local.handle_locations_peer("bacloud", {"after": "", "limit": 100})
    assert presence["ok"] is True
    assert {item["public_name"] for item in presence["recipients"]} == {
        _public_name(local.mesh.store.slots["alice"]),
        _public_name(local.mesh.store.slots["carol"]),
    }
    with local.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM access_mesh_message_outbox").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_local_ack_once_no_duplicate_receipt_event_or_expiry_renewal(mesh_pair):
    local = mesh_pair.nodes["firstbyte"]
    recipient = _public_name(local.mesh.store.slots["carol"])
    sent = await local.message(actor("alice"), text="exactly one ACK", target=recipient, mode="ack")
    assert sent["ok"]
    message_id = sent["message_hash"]
    created = local.store.wire(message_id)["created_at"]
    before = local.mesh.now
    # Keep the test identity alive so its ACK contract is isolated from the
    # independent 15-minute work-session expiry policy.
    for slot in local.mesh.store.slots.values():
        slot.expires_at = before + timedelta(days=2)
    first = await local.message(actor("carol"), message_hash=message_id)
    assert first["ok"]
    with local.store.connect() as db:
        first_receipt = db.execute(
            "SELECT read_at FROM coordination_message_recipients "
            "WHERE message_hash=? AND recipient_agent_id='carol'",
            (message_id,),
        ).fetchone()[0]
        event_count = db.execute(
            "SELECT count(*) FROM instance_events WHERE event_type='message.receipt' "
            "AND entity_id=?",
            (message_id,),
        ).fetchone()[0]
    local.mesh.now = before + timedelta(hours=6)
    again = await local.message(actor("carol"), message_hash=message_id)
    assert again["ok"] and again["state"] == first["state"]
    with local.store.connect() as db:
        assert (
            db.execute(
                "SELECT read_at FROM coordination_message_recipients "
                "WHERE message_hash=? AND recipient_agent_id='carol'",
                (message_id,),
            ).fetchone()[0]
            == first_receipt
        )
        assert (
            db.execute(
                "SELECT count(*) FROM instance_events WHERE event_type='message.receipt' "
                "AND entity_id=?",
                (message_id,),
            ).fetchone()[0]
            == event_count
        )
    assert local.store.wire(message_id)["created_at"] == created
    local.mesh.now = before + timedelta(hours=24)
    assert local.store.wire(message_id) is None


@pytest.mark.asyncio
async def test_local_health_probe_is_transactional_and_detects_missing_storage(mesh_pair):
    local = mesh_pair.nodes["firstbyte"]
    tables = (
        "coordination_messages",
        "coordination_message_recipients",
        "access_mesh_messages",
        "instance_events",
        "access_mesh_message_outbox",
    )
    with local.store.connect() as db:
        counts_before = [
            db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tables
        ]
    assert local.store.health_probe() is True
    assert local.store.health_probe() is True
    with local.store.connect() as db:
        counts_after = [
            db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tables
        ]
    assert counts_before == counts_after
    with local.store.connect() as db:
        db.execute("DROP TABLE access_mesh_messages")  # isolated test DB only
    import sqlite3

    with pytest.raises(sqlite3.OperationalError):
        local.store.health_probe()


@pytest.mark.asyncio
async def test_maximum_supported_node_id_preserves_public_message_id_budget(mesh_pair):
    """Origin node up to 80 chars must fit the public 128-char message limit."""
    local = mesh_pair.nodes["firstbyte"]
    long_node_id = "n" * 80
    local.mesh.store.local_node_id = long_node_id
    local.store.local_node_id = long_node_id
    local.store.local_prefix = f"{long_node_id}:localmsg:"
    recipient = _public_name(local.mesh.store.slots["carol"])
    sent = await local.message(actor("alice"), text="node boundary", target=recipient)
    assert sent["ok"], sent
    assert sent["message_hash"].startswith(local.store.local_prefix)
    assert len(sent["message_hash"]) <= 128
    assert local.store.wire(sent["message_hash"])["text"] == "node boundary"
    slot = SimpleNamespace(issuer_id="issuer", slot_id="slot")
    alert_id = AccessMeshMessaging._session_alert_hash(long_node_id, slot, "2026-10-10T00:00:00Z")
    assert len(alert_id) <= 128
    assert alert_id.startswith(local.store.local_prefix)
