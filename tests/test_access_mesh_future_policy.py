"""Next-window policy changes keep active grants and peer clocks stable."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from terminal_mcp.core.access_mesh_grants import AccessSlotEvent, LocalAccessMesh, SlotPolicy
from terminal_mcp.core.access_policy_windows import capture_window, slot_cycle

T0 = datetime(2026, 10, 1, tzinfo=UTC)
OLD = SlotPolicy(duration_seconds=60, warning_seconds=10, draining_seconds=5, cooldown_seconds=15)
LONG = SlotPolicy(
    duration_seconds=10800, warning_seconds=180, draining_seconds=30, cooldown_seconds=120
)


def stamp(seconds):
    return T0 + timedelta(seconds=seconds)


def replica(tmp_path, *, policy=OLD, start=T0):
    mesh = LocalAccessMesh(
        tmp_path / "replica.sqlite",
        local_node_id="issuer",
        trusted_issuers=frozenset({"issuer"}),
        proof_key=b"synthetic-proof-for-isolated-tests",
    )
    event = AccessSlotEvent(
        "issuer",
        "slot_test",
        "agent_test",
        1,
        "event_issue",
        "SlotIssued",
        policy,
        mesh.code_tag("issuer", "0000"),
        start,
    )
    mesh.apply_event(event, authenticated_peer_id="issuer", issued_kind="persistent")
    return mesh


def edit(mesh, when, *, policy=LONG):
    slot = mesh.slot("issuer", "slot_test")
    event = AccessSlotEvent(
        "issuer",
        slot.slot_id,
        slot.logical_agent_id,
        slot.revision + 1,
        "policy_" + str(slot.revision + 1),
        "SlotPolicyChanged",
        policy=policy,
        policy_window=capture_window(slot, stamp(when)),
    )
    mesh.apply_event(event, authenticated_peer_id="issuer")
    return mesh.slot("issuer", "slot_test"), event


def assert_time(value, expected):
    assert datetime.fromisoformat(value) == stamp(expected)


def test_policy_edit_preserves_current_window(tmp_path):
    mesh = replica(tmp_path)

    slot, _ = edit(mesh, 20)

    assert slot.policy == LONG
    assert slot.effective(stamp(20)).policy == OLD
    assert_time(slot_cycle(slot, stamp(20))["hard_expires_at"], 60)


def test_policy_capture_uses_current_automatic_cycle(tmp_path):
    mesh = replica(tmp_path)

    slot, _ = edit(mesh, 200)

    assert_time(slot_cycle(slot, stamp(200))["started_at"], 150)
    assert_time(slot_cycle(slot, stamp(200))["hard_expires_at"], 210)


def test_policy_edit_preserves_captured_reopen_time(tmp_path):
    mesh = replica(tmp_path)

    slot, _ = edit(mesh, 65)

    assert_time(slot_cycle(slot, stamp(66))["rearm_at"], 75)


def test_idle_slot_does_not_start_as_side_effect_of_policy_edit(tmp_path):
    mesh = replica(tmp_path, start=None)
    slot, _ = edit(mesh, 20)
    assert slot_cycle(slot, stamp(20))["state"] == "idle"
    assert not slot.writable_at(stamp(100000))


def test_next_window_uses_updated_policy(tmp_path):
    mesh = replica(tmp_path)
    policy = replace(LONG, rearm_enabled=False)
    slot, _ = edit(mesh, 20, policy=policy)

    assert slot.effective(stamp(75)).policy == policy


def test_deadline_override_retains_current_thresholds(tmp_path):
    mesh = replica(tmp_path)
    slot, _ = edit(mesh, 20)
    event = AccessSlotEvent(
        "issuer",
        slot.slot_id,
        slot.logical_agent_id,
        3,
        "window_extend",
        "SessionUpdated",
        effective_at=T0,
        deadline_at=stamp(90),
    )

    mesh.apply_event(event, authenticated_peer_id="issuer")

    current = mesh.slot("issuer", slot.slot_id)
    assert current.effective(stamp(50)).policy == OLD
    assert_time(slot_cycle(current, stamp(50))["hard_expires_at"], 90)
