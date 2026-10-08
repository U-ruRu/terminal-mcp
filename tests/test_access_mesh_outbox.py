"""Durable two-issuer event delivery without a central per-write dependency."""

from datetime import UTC, datetime, timedelta

import pytest

from terminal_mcp.core.access_mesh_grants import (
    AccessMeshError,
    AccessSlotEvent,
    LocalAccessMesh,
    SlotPolicy,
)

T0 = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
PROOF_KEY = b"mesh-event-unit-fixture-not-for-production"


def node(tmp_path, name):
    return LocalAccessMesh(
        tmp_path / f"{name}.db",
        local_node_id=name,
        trusted_issuers=frozenset({"firstbyte", "bacloud"}),
        proof_key=PROOF_KEY,
    )


def new_slot(store, *, code="1234", slot="agent-slot", kind="legacy"):
    return AccessSlotEvent(
        issuer_id=store.local_node_id,
        slot_id=slot,
        logical_agent_id=f"agent-{store.local_node_id}",
        revision=1,
        event_id=f"{store.local_node_id}:{slot}:1",
        kind="SlotIssued",
        policy=SlotPolicy(duration_seconds=20, cooldown_seconds=10),
        code_tag=store.code_tag(store.local_node_id, code),
        effective_at=T0,
    )


def changed(previous, revision, kind, *, when=None):
    return AccessSlotEvent(
        issuer_id=previous.issuer_id,
        slot_id=previous.slot_id,
        logical_agent_id=previous.logical_agent_id,
        revision=revision,
        event_id=f"{previous.issuer_id}:{previous.slot_id}:{revision}",
        kind=kind,
        effective_at=when,
    )


def deliver_one(issuer, consumer, event, kind=None):
    return consumer.apply_event(
        event, authenticated_peer_id=issuer.local_node_id, issued_kind=kind,
    )


def test_two_issuers_issue_and_consume_opposite_directions(tmp_path):
    fb = node(tmp_path, "firstbyte")
    bac = node(tmp_path, "bacloud")
    for issuer, consumer in ((fb, bac), (bac, fb)):
        slot = new_slot(issuer, code="1010")
        issuer.apply_event(
            slot,
            authenticated_peer_id=issuer.local_node_id,
            issued_kind="legacy",
            broadcast_to=(consumer.local_node_id,),
        )
        pending = issuer.pending_outbox(peer_node_id=consumer.local_node_id)
        assert len(pending) == 1
        assert pending[0].event == slot
        assert pending[0].issued_kind == "legacy"
        result = deliver_one(issuer, consumer, pending[0].event, pending[0].issued_kind)
        assert result.outcome == "applied"
        local_actor = f"role-executor:{consumer.local_node_id}"
        attached = consumer.attach(
            issuer_id=issuer.local_node_id, code="1010", connection_key=local_actor,
        )
        assert attached.logical_agent_id == slot.logical_agent_id
        assert consumer.may_write(connection_key=local_actor, now=T0 + timedelta(seconds=1))
        assert issuer.acknowledge_delivery(
            peer_node_id=consumer.local_node_id,
            event_id=slot.event_id,
            authenticated_peer_id=consumer.local_node_id,
        )
        assert issuer.pending_outbox(peer_node_id=consumer.local_node_id) == ()


def test_outbox_survives_restart_and_repeated_delivery_is_idempotent(tmp_path):
    issuer = node(tmp_path, "firstbyte")
    consumer = node(tmp_path, "bacloud")
    slot = new_slot(issuer, code="9876")
    issuer.apply_event(
        slot, authenticated_peer_id="firstbyte", issued_kind="persistent",
        broadcast_to=("bacloud",),
    )
    reopened = node(tmp_path, "firstbyte")
    receipt = reopened.pending_outbox(peer_node_id="bacloud")[0]
    assert deliver_one(reopened, consumer, receipt.event, receipt.issued_kind).outcome == "applied"
    repeated = deliver_one(reopened, consumer, receipt.event, receipt.issued_kind)
    assert repeated.outcome == "duplicate"
    assert reopened.acknowledge_delivery(
        peer_node_id="bacloud", event_id=slot.event_id,
        authenticated_peer_id="bacloud",
    )
    assert reopened.acknowledge_delivery(
        peer_node_id="bacloud", event_id=slot.event_id,
        authenticated_peer_id="bacloud",
    )
    assert reopened.pending_outbox(peer_node_id="bacloud") == ()


def test_out_of_order_events_require_replay_not_state_corruption(tmp_path):
    issuer = node(tmp_path, "bacloud")
    consumer = node(tmp_path, "firstbyte")
    first = new_slot(issuer)
    issuer.apply_event(
        first, authenticated_peer_id="bacloud", issued_kind="legacy",
        broadcast_to=("firstbyte",),
    )
    issuer.apply_event(
        changed(first, 2, "SlotSuspended"),
        authenticated_peer_id="bacloud", broadcast_to=("firstbyte",),
    )
    issuer.apply_event(
        changed(first, 3, "SlotResumed"),
        authenticated_peer_id="bacloud", broadcast_to=("firstbyte",),
    )
    pending = issuer.pending_outbox(peer_node_id="firstbyte")
    assert [x.event.revision for x in pending] == [1, 2, 3]
    with pytest.raises(AccessMeshError, match="access_mesh_unknown_slot"):
        deliver_one(issuer, consumer, pending[2].event)
    assert consumer.slot("bacloud", first.slot_id) is None
    deliver_one(issuer, consumer, pending[0].event, pending[0].issued_kind)
    with pytest.raises(AccessMeshError, match="access_mesh_event_gap"):
        deliver_one(issuer, consumer, pending[2].event)
    assert consumer.slot("bacloud", first.slot_id).revision == 1
    deliver_one(issuer, consumer, pending[1].event)
    deliver_one(issuer, consumer, pending[2].event)
    assert consumer.slot("bacloud", first.slot_id).state == "active"
    assert consumer.slot("bacloud", first.slot_id).revision == 3


def test_issuer_event_update_and_legacy_claim_release_intent(tmp_path):
    fb = node(tmp_path, "firstbyte")
    bac = node(tmp_path, "bacloud")
    first = new_slot(fb)
    fb.apply_event(
        first, authenticated_peer_id="firstbyte", issued_kind="legacy",
        broadcast_to=("bacloud",),
    )
    pending = fb.pending_outbox(peer_node_id="bacloud")[0]
    deliver_one(fb, bac, pending.event, pending.issued_kind)
    fb.acknowledge_delivery(
        peer_node_id="bacloud", event_id=first.event_id, authenticated_peer_id="bacloud",
    )
    end = changed(first, 2, "SessionEnded", when=T0 + timedelta(seconds=5))
    fb.apply_event(end, authenticated_peer_id="firstbyte", broadcast_to=("bacloud",))
    pending = fb.pending_outbox(peer_node_id="bacloud")
    assert len(pending) == 1
    receiver = deliver_one(fb, bac, pending[0].event)
    assert receiver.release_claims
    assert receiver.logical_agent_id == "agent-firstbyte"
    assert bac.slot("firstbyte", first.slot_id).anchor == T0 + timedelta(seconds=15)


def test_disallowed_peers_cannot_enqueue_or_ack(tmp_path):
    fb = node(tmp_path, "firstbyte")
    first = new_slot(fb)
    with pytest.raises(AccessMeshError, match="access_mesh_untrusted_peer"):
        fb.apply_event(
            first, authenticated_peer_id="firstbyte", issued_kind="legacy",
            broadcast_to=("tokyo",),
        )
    assert fb.slot("firstbyte", first.slot_id) is None
    assert fb.pending_outbox(peer_node_id="bacloud") == ()
    fb.apply_event(
        first, authenticated_peer_id="firstbyte", issued_kind="legacy",
        broadcast_to=("bacloud",),
    )
    with pytest.raises(AccessMeshError, match="access_mesh_untrusted_peer"):
        fb.acknowledge_delivery(
            peer_node_id="bacloud", event_id=first.event_id,
            authenticated_peer_id="tokyo",
        )
    assert len(fb.pending_outbox(peer_node_id="bacloud")) == 1
    assert not fb.acknowledge_delivery(
        peer_node_id="bacloud", event_id="unknown-event",
        authenticated_peer_id="bacloud",
    )


def test_wire_format_is_strict_and_can_be_round_tripped(tmp_path):
    store = node(tmp_path, "firstbyte")
    event = new_slot(store)
    wire = event.to_wire()
    assert "1234" not in str(wire)
    assert AccessSlotEvent.from_wire(wire) == event
    with pytest.raises(AccessMeshError, match="access_mesh_invalid_wire_event"):
        AccessSlotEvent.from_wire({**wire, "extra": "forbidden"})
    with pytest.raises(AccessMeshError, match="access_mesh_invalid_wire_event"):
        AccessSlotEvent.from_wire({**wire, "effective_at": 123})
    with pytest.raises(AccessMeshError, match="access_mesh_invalid_wire_event"):
        AccessSlotEvent.from_wire({**wire, "policy": {"duration_seconds": -1}})


def test_outbox_limit_and_peer_scope_are_validated(tmp_path):
    fb = node(tmp_path, "firstbyte")
    first = new_slot(fb)
    fb.apply_event(
        first, authenticated_peer_id="firstbyte", issued_kind="legacy",
        broadcast_to=("bacloud", "bacloud"),
    )
    assert len(fb.pending_outbox(peer_node_id="bacloud", limit=1)) == 1
    with pytest.raises(AccessMeshError, match="access_mesh_invalid_peer"):
        fb.pending_outbox(peer_node_id="firstbyte")
    with pytest.raises(AccessMeshError, match="access_mesh_invalid_peer"):
        fb.pending_outbox(peer_node_id="bacloud", limit=0)
    with pytest.raises(AccessMeshError, match="access_mesh_invalid_peer"):
        fb.pending_outbox(peer_node_id="bacloud", limit=True)
