"""QA regression for bounded reuse of Mesh numbers after session expiry."""

from datetime import UTC, datetime, timedelta

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
