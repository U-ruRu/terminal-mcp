"""Issuer-authoritative start activation metadata under delayed Mesh replication."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_access_mesh_replication_http import Routes, actor, node

from terminal_mcp.core.access_mesh_grants import AccessMeshError, SlotPolicy
from terminal_mcp.storage.access_mesh import AccessMeshStore

T0 = datetime(2026, 10, 10, 7, 43, 19, tzinfo=UTC)
PROOF = b"QA-mesh-start-issuer-key-32bytes!!"


def test_issuer_activation_repairs_only_stale_replicas(tmp_path):
    original = {
        "event_id": "ae_start_that_was_delayed",
        "number": "0493",
        "issuer_id": "firstbyte",
        "slot_id": "original_slot",
        "active_from": T0 + timedelta(minutes=10, seconds=1),
        "started_at": T0,
        "hard_expires_at": T0 + timedelta(minutes=20),
    }
    mismatch = {**original, "active_from": original["active_from"] + timedelta(seconds=3)}
    issuer = AccessMeshStore(
        tmp_path / "firstbyte.sqlite3",
        local_node_id="firstbyte",
        trusted_issuers=frozenset({"firstbyte", "bacloud"}),
        proof_key=PROOF,
    )
    replica = AccessMeshStore(
        tmp_path / "bacloud.sqlite3",
        local_node_id="bacloud",
        trusted_issuers=frozenset({"firstbyte", "bacloud"}),
        proof_key=PROOF,
    )
    issuer.numbers.record_start(**original)
    replica.numbers.record_start(**mismatch)

    # A relay of the mistaken replica must never rewrite the real issuer's event.
    assert issuer.numbers.record_start(**mismatch, source_peer_id="bacloud")["ok"]
    assert issuer.numbers.start_snapshot()[0]["active_from"] == issuer.numbers.stamp(
        original["active_from"]
    )
    # An authenticated original issuer repairs the stale replica projection.
    assert replica.numbers.record_start(**original, source_peer_id="firstbyte")["ok"]
    assert replica.numbers.start_snapshot() == issuer.numbers.start_snapshot()
    # Repeating an identical event is a strict no-op.
    assert replica.numbers.record_start(**original, source_peer_id="firstbyte")["ok"]
    assert replica.numbers.start_snapshot() == issuer.numbers.start_snapshot()
    # Tampered non-time metadata is still a hard idempotency conflict.
    with pytest.raises(AccessMeshError, match="idempotency_conflict"):
        replica.numbers.record_start(
            **{**original, "hard_expires_at": T0 + timedelta(minutes=21)},
            source_peer_id="firstbyte",
        )


@pytest.mark.asyncio
async def test_replica_sessionstarted_event_does_not_invent_manual_activation(tmp_path):
    """The Access event itself lacks active_from; /numbers/starts is authoritative."""
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        first, first_rep = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        second, second_rep = await node(tmp_path, "bacloud", "firstbyte", routes, client)
        now_issuer = T0 + timedelta(minutes=10, seconds=1)
        now_replica = now_issuer + timedelta(seconds=3)
        first.clock = lambda: now_issuer
        first.store.clock = first.clock
        second.clock = lambda: now_replica
        second.store.clock = second.clock
        issued = await first.issue(actor(), code="0493", policy=SlotPolicy(1200))
        await first_rep.tick()
        await second_rep.tick()
        await first.change(
            actor(),
            slot_id=issued["slot_id"],
            kind="SessionStarted",
            expected_revision=1,
            effective_at=T0,
            deadline_at=T0 + timedelta(minutes=20),
            number_start={"active_from": now_issuer},
        )
        assert first.store.numbers.start_snapshot()[0]["active_from"] == first.store.numbers.stamp(
            now_issuer
        )
        # Deliver just the issuer event, before independent /numbers/starts metadata.
        await first_rep._deliver(first_rep.peers[0])
        assert second.store.numbers.start_snapshot() == []
        await second_rep.sync_starts(second_rep.peers[0])
        assert second.store.numbers.start_snapshot() == first.store.numbers.start_snapshot()
        await first_rep.tick()
        await second_rep.tick()
        assert first_rep.peer_health["bacloud"]["status"] == "healthy"
        assert second_rep.peer_health["firstbyte"]["status"] == "healthy"


@pytest.mark.asyncio
async def test_local_start_without_explicit_number_metadata_keeps_live_activation(tmp_path):
    """Legacy-compatible local starts still use the current clock, not old anchor."""
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        first, _ = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        live_activation = T0 + timedelta(minutes=10, seconds=1)
        first.clock = lambda: live_activation
        first.store.clock = first.clock
        issued = await first.issue(actor(), code="0494", policy=SlotPolicy(1200))
        await first.change(
            actor(), slot_id=issued["slot_id"], kind="SessionStarted",
            expected_revision=1, effective_at=T0,
            deadline_at=T0 + timedelta(minutes=20),
        )
        row = first.store.numbers.start_snapshot()[0]
        assert row["active_from"] == first.store.numbers.stamp(live_activation)
        assert row["started_at"] == first.store.numbers.stamp(T0)
