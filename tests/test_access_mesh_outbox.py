"""Durable two-issuer event delivery without a central per-write dependency."""

from datetime import UTC, datetime

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


def new_slot(store, *, code="1234", slot="agent-slot", kind="temporary"):
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
        event,
        authenticated_peer_id=issuer.local_node_id,
        issued_kind=kind,
    )


def test_pending_event_survives_store_reopen(tmp_path):
    issuer = node(tmp_path, "firstbyte")
    event = new_slot(issuer)
    issuer.apply_event(
        event,
        authenticated_peer_id="firstbyte",
        issued_kind="persistent",
        broadcast_to=("bacloud",),
    )
    reopened = node(tmp_path, "firstbyte")
    pending = reopened.pending_outbox(peer_node_id="bacloud")
    assert len(pending) == 1
    assert (pending[0].event, pending[0].issued_kind) == (event, "persistent")


def test_authenticated_ack_completes_pending_delivery(tmp_path):
    issuer = node(tmp_path, "firstbyte")
    event = new_slot(issuer)
    issuer.apply_event(
        event,
        authenticated_peer_id="firstbyte",
        issued_kind="persistent",
        broadcast_to=("bacloud",),
    )
    assert issuer.acknowledge_delivery(
        peer_node_id="bacloud", event_id=event.event_id, authenticated_peer_id="bacloud"
    )
    assert issuer.pending_outbox(peer_node_id="bacloud") == ()


def test_other_peer_cannot_acknowledge_pending_delivery(tmp_path):
    issuer = node(tmp_path, "firstbyte")
    event = new_slot(issuer)
    issuer.apply_event(
        event,
        authenticated_peer_id="firstbyte",
        issued_kind="persistent",
        broadcast_to=("bacloud",),
    )
    with pytest.raises(AccessMeshError, match="access_mesh_untrusted_peer"):
        issuer.acknowledge_delivery(
            peer_node_id="bacloud", event_id=event.event_id, authenticated_peer_id="tokyo"
        )
    assert issuer.pending_outbox(peer_node_id="bacloud")[0].event == event


def test_current_event_survives_wire_round_trip(tmp_path):
    event = new_slot(node(tmp_path, "firstbyte"))
    assert AccessSlotEvent.from_wire(event.to_wire()) == event
