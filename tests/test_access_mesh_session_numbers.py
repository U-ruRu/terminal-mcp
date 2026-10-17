"""Collision and reservation invariants for partition-tolerant Mesh numbers."""

from datetime import UTC, datetime, timedelta

import pytest

from terminal_mcp.core.access_mesh_grants import AccessMeshError
from terminal_mcp.storage.access_mesh import AccessMeshStore

T0 = datetime(2026, 10, 9, tzinfo=UTC)


@pytest.fixture
def registry(tmp_path):
    store = AccessMeshStore(
        tmp_path / "numbers.sqlite3",
        local_node_id="a",
        trusted_issuers=frozenset({"a", "b"}),
        proof_key=b"unit-test-mesh-secret-key-32bytes",
    )
    return store.numbers


def test_reservation_retry_preserves_original_expiry(registry):
    registry.reserve(number="0000", attempt_id="attempt-a", now=T0)
    now = T0 + timedelta(minutes=2, seconds=50)
    assert registry.reserve(number="0000", attempt_id="attempt-a", now=now)["ok"]
    assert registry.reservations(now=now)[0]["expires_at"] == registry.stamp(
        T0 + timedelta(minutes=3)
    )


def test_collision_selects_latest_identity_with_maximum_deadline(registry):
    a = dict(
        number="0001",
        issuer_id="a",
        slot_id="s1",
        logical_agent_id="la_a",
        started_at=T0,
        hard_expires_at=T0 + timedelta(minutes=10),
    )
    b = dict(
        number="0001",
        issuer_id="b",
        slot_id="s2",
        logical_agent_id="la_b",
        started_at=T0 + timedelta(minutes=1),
        hard_expires_at=T0 + timedelta(minutes=4),
    )
    registry.register(**a)
    assert registry.register(**b)["collisions"] == 1
    assert registry.winner("0001")["logical_agent_id"] == "la_b"
    assert registry.winner("0001")["hard_expires_at"] == registry.stamp(a["hard_expires_at"])


def test_timestamp_tie_uses_stable_issuer_and_slot_order(registry):
    for issuer, slot in [("a", "1"), ("b", "2")]:
        registry.register(
            number="1234",
            issuer_id=issuer,
            slot_id=slot,
            logical_agent_id="la_" + issuer,
            started_at=T0,
            hard_expires_at=T0 + timedelta(minutes=5),
        )
    assert registry.winner("1234")["logical_agent_id"] == "la_b"


def test_claims_survive_store_reopen(tmp_path):
    path = tmp_path / "reopen.sqlite3"
    kwargs = dict(
        local_node_id="one",
        trusted_issuers=frozenset({"one"}),
        proof_key=b"unit-test-mesh-secret-key-32bytes",
    )
    original = AccessMeshStore(path, **kwargs)
    original.numbers.register(
        number="0037",
        issuer_id="one",
        slot_id="slot",
        logical_agent_id="la_one",
        started_at=T0,
        hard_expires_at=T0 + timedelta(minutes=23),
    )
    reopened = AccessMeshStore(path, **kwargs)
    assert reopened.numbers.winner("0037")["logical_agent_id"] == "la_one"
    assert reopened.numbers.winner("0037")["hard_expires_at"] == reopened.numbers.stamp(
        T0 + timedelta(minutes=23)
    )


def test_recycled_number_forms_new_identity_epoch_and_retains_old_history(registry):
    old = dict(
        number="0789",
        issuer_id="a",
        slot_id="old",
        logical_agent_id="historical-agent",
        started_at=T0,
        hard_expires_at=T0 + timedelta(minutes=5),
    )
    new = dict(
        number="0789",
        issuer_id="b",
        slot_id="new",
        logical_agent_id="new-agent",
        started_at=T0 + timedelta(minutes=6),
        hard_expires_at=T0 + timedelta(minutes=10),
    )
    registry.register(**old)
    fresh = registry.register(**new)
    assert fresh["collisions"] == 0
    assert registry.winner("0789")["logical_agent_id"] == "new-agent"
    assert registry.winner("0789")["hard_expires_at"] == registry.stamp(new["hard_expires_at"])
    assert registry.group_winner("a", "old")["logical_agent_id"] == "historical-agent"
    assert registry.group_winner("b", "new")["logical_agent_id"] == "new-agent"


def test_transitive_overlap_chooses_latest_started_and_merged_deadline(registry):
    for issuer, slot, start, deadline in [
        ("a", "A", 0, 10),
        ("b", "B", 9, 15),
        ("a", "C", 14, 17),
    ]:
        registry.register(
            number="0765",
            issuer_id=issuer,
            slot_id=slot,
            logical_agent_id=f"la-{slot}",
            started_at=T0 + timedelta(minutes=start),
            hard_expires_at=T0 + timedelta(minutes=deadline),
        )
    assert registry.winner("0765")["logical_agent_id"] == "la-C"
    assert registry.group_winner("a", "A")["logical_agent_id"] == "la-C"
    assert registry.group_winner("b", "B")["hard_expires_at"] == registry.stamp(
        T0 + timedelta(minutes=17)
    )


def test_start_replay_is_exact_and_cannot_shift_activation_timestamp(registry):
    start = {
        "number": "0044",
        "issuer_id": "a",
        "slot_id": "slot",
        "event_id": "ae_epoch_start",
        "active_from": T0,
        "started_at": T0,
        "hard_expires_at": T0 + timedelta(minutes=5),
    }
    assert registry.record_start(**start)["ok"]
    before = registry.start_snapshot()
    with pytest.raises(AccessMeshError, match="idempotency_conflict"):
        registry.record_start(
            **{**start, "active_from": T0 + timedelta(minutes=3)},
        )
    assert registry.start_snapshot() == before
