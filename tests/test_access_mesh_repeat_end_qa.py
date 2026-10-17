"""Separate end events in a current numbered cycle retain independent receipts."""

from test_access_mesh_session_numbers import registry as registry


def test_separate_end_events_are_recorded_in_one_cycle(registry):
    cycle = dict(
        number="0319",
        cycle_key="a:slot:2026-10-09T10:00:00+00:00",
        issuer_id="a",
        slot_id="slot",
    )
    registry.record_end(**cycle, event_id="first-end", ended_at="2026-10-09T10:00:05+00:00")
    result = registry.record_end(
        **cycle, event_id="second-end", ended_at="2026-10-09T10:00:15+00:00"
    )
    assert result["ok"]
    assert {row["event_id"] for row in registry.end_snapshot()} == {"first-end", "second-end"}
