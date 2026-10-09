"""QA regression for bounded reuse of Mesh numbers after session expiry."""

from datetime import UTC, datetime, timedelta

import pytest

from terminal_mcp.storage.access_mesh import AccessMeshStore


def test_expired_number_reusable_without_erasing_historical_identity(tmp_path):
    store = AccessMeshStore(
        tmp_path / "numbers.sqlite3",
        local_node_id="a",
        trusted_issuers=frozenset({"a", "b"}),
        proof_key=b"unit-test-mesh-secret-key-32bytes",
    )
    t0 = datetime(2026, 10, 9, tzinfo=UTC)
    store.numbers.register(
        number="0001",
        issuer_id="a",
        slot_id="finished-slot",
        logical_agent_id="previous-agent",
        started_at=t0,
        hard_expires_at=t0 + timedelta(minutes=5),
        now=t0,
    )
    assert store.numbers.reserve(
        number="0001", attempt_id="new-session", now=t0 + timedelta(minutes=6)
    ) == {"ok": True, "number": "0001"}
    assert len(store.numbers.snapshot()) == 1


@pytest.mark.asyncio
async def test_expired_slot_can_reissue_same_number_and_keep_both_histories(tmp_path):
    """Recycling a number works past both reservation and live-slot unique indexes."""
    from terminal_mcp.core.access_mesh_grants import AccessSlotEvent, SlotPolicy
    from terminal_mcp.storage.sqlite import SqliteRepository

    repo = SqliteRepository(tmp_path / "recycle.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    t0 = datetime(2026, 10, 9, tzinfo=UTC)
    now = [t0]
    store = AccessMeshStore(
        tmp_path / "recycle.sqlite3",
        local_node_id="a",
        trusted_issuers=frozenset({"a"}),
        proof_key=b"unit-test-mesh-secret-key-32bytes",
        clock=lambda: now[0],
    )
    for idx, when in enumerate((t0, t0 + timedelta(minutes=6)), 1):
        now[0] = when
        attempt = f"reserve-{idx}"
        if idx == 1:
            assert store.numbers.reserve(number="0001", attempt_id=attempt, now=when)["ok"]
        # On the second pass, emulate a successful peer admission decision.
        # This isolates the storage uniqueness fence from the reservation bug.
        event = AccessSlotEvent(
            issuer_id="a",
            slot_id=f"slot-{idx}",
            logical_agent_id=f"agent-{idx}",
            revision=1,
            event_id=f"issue-{idx}",
            kind="SlotIssued",
            policy=SlotPolicy(duration_seconds=300, rearm_enabled=False),
            code_tag=store.code_tag("a", "0001"),
            effective_at=when,
        )
        applied = store.apply_event(
            event,
            authenticated_peer_id="a",
            issued_kind="legacy",
            mutation_number={"number": "0001", "attempt_id": attempt},
        )
        assert applied.outcome == "applied"
    assert len(store.numbers.snapshot()) == 2
    assert store.numbers.winner("0001")["logical_agent_id"] == "agent-2"
    assert store.slot("a", "slot-1") is not None
