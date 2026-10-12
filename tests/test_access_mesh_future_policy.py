"""Next-window policy changes keep active grants and peer clocks stable."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from terminal_mcp.core.access_mesh_grants import (
    AccessMeshError,
    AccessSlotEvent,
    CapturedAccessWindow,
    LocalAccessMesh,
    SlotPolicy,
)
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


def test_three_hours_applies_to_next_window_not_current(tmp_path):
    mesh = replica(tmp_path)
    slot, _ = edit(mesh, 20)
    assert slot.policy.duration_seconds == 10800
    assert slot.effective(stamp(20)).policy == OLD
    assert_time(slot_cycle(slot, stamp(20))["hard_expires_at"], 60)
    assert slot_cycle(slot, stamp(55))["state"] == "draining"
    assert not slot.writable_at(stamp(65))
    assert slot.writable_at(stamp(75))
    assert slot.effective(stamp(75)).policy == LONG
    assert_time(slot_cycle(slot, stamp(75))["hard_expires_at"], 10875)


def test_second_edit_preserves_original_current_window_and_cooldown(tmp_path):
    mesh = replica(tmp_path)
    edit(mesh, 20)
    shorter = replace(LONG, duration_seconds=600)
    slot, _ = edit(mesh, 35, policy=shorter)
    assert_time(slot_cycle(slot, stamp(35))["hard_expires_at"], 60)
    assert_time(slot_cycle(slot, stamp(70))["rearm_at"], 75)
    assert_time(slot_cycle(slot, stamp(75))["hard_expires_at"], 675)


def test_capture_after_automatic_rearm_uses_actual_cycle_not_first_anchor(tmp_path):
    mesh = replica(tmp_path)
    slot, _ = edit(mesh, 200)
    assert_time(slot_cycle(slot, stamp(200))["started_at"], 150)
    assert_time(slot_cycle(slot, stamp(200))["hard_expires_at"], 210)
    assert_time(slot_cycle(slot, stamp(225))["hard_expires_at"], 11025)


def test_edit_during_cooldown_preserves_reopen_time(tmp_path):
    mesh = replica(tmp_path)
    slot, _ = edit(mesh, 65)
    assert_time(slot_cycle(slot, stamp(66))["rearm_at"], 75)
    assert_time(slot_cycle(slot, stamp(75))["hard_expires_at"], 10875)


def test_idle_slot_does_not_start_as_side_effect_of_policy_edit(tmp_path):
    mesh = replica(tmp_path, start=None)
    slot, _ = edit(mesh, 20)
    assert slot_cycle(slot, stamp(20))["state"] == "idle"
    assert not slot.writable_at(stamp(100000))


def test_existing_nonrearming_window_stays_expired_until_explicit_start(tmp_path):
    mesh = replica(tmp_path, policy=replace(OLD, rearm_enabled=False))
    slot, _ = edit(mesh, 20)
    assert slot.writable_at(stamp(59))
    assert not slot.writable_at(stamp(75))
    start = AccessSlotEvent(
        "issuer",
        slot.slot_id,
        slot.logical_agent_id,
        3,
        "explicit_start",
        "SessionStarted",
        effective_at=stamp(100),
    )
    mesh.apply_event(start, authenticated_peer_id="issuer")
    current = mesh.slot("issuer", slot.slot_id)
    assert current.policy_window is None
    assert_time(slot_cycle(current, stamp(100))["hard_expires_at"], 10900)


def test_captured_rearm_opens_one_next_window_with_new_nonrearming_defaults(tmp_path):
    mesh = replica(tmp_path)
    slot, _ = edit(mesh, 20, policy=replace(LONG, rearm_enabled=False))
    assert slot.writable_at(stamp(75))
    assert not slot.writable_at(stamp(10875))


def test_explicit_window_action_changes_deadline_but_keeps_current_thresholds(tmp_path):
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
    assert slot_cycle(current, stamp(85))["state"] == "draining"
    assert_time(slot_cycle(current, stamp(105))["hard_expires_at"], 10905)


def test_explicit_end_keeps_captured_cooldown_then_uses_new_defaults(tmp_path):
    mesh = replica(tmp_path)
    slot, _ = edit(mesh, 20)
    event = AccessSlotEvent(
        "issuer",
        slot.slot_id,
        slot.logical_agent_id,
        3,
        "explicit_end",
        "SessionEnded",
        effective_at=stamp(30),
    )
    mesh.apply_event(event, authenticated_peer_id="issuer")
    current = mesh.slot("issuer", slot.slot_id)
    assert not current.writable_at(stamp(40))
    assert_time(slot_cycle(current, stamp(40))["rearm_at"], 45)
    assert_time(slot_cycle(current, stamp(45))["hard_expires_at"], 10845)


def test_delayed_peer_receives_same_window_and_future_schedule(tmp_path):
    mesh = replica(tmp_path)
    before = mesh.slot("issuer", "slot_test")
    slot, event = edit(mesh, 20)
    remote = LocalAccessMesh(
        tmp_path / "remote.sqlite",
        local_node_id="replica",
        trusted_issuers=frozenset({"issuer"}),
        proof_key=b"synthetic-proof-for-isolated-tests",
    )
    remote.apply_event(
        AccessSlotEvent(
            "issuer",
            before.slot_id,
            before.logical_agent_id,
            1,
            "event_issue",
            "SlotIssued",
            OLD,
            before.code_tag,
            T0,
        ),
        authenticated_peer_id="issuer",
        issued_kind="persistent",
    )
    remote.apply_event(AccessSlotEvent.from_wire(event.to_wire()), authenticated_peer_id="issuer")
    received = remote.slot("issuer", slot.slot_id)
    for second in (20, 55, 60, 74, 75, 100, 10875, 10995):
        assert slot_cycle(received, stamp(second)) == slot_cycle(slot, stamp(second))
        assert received.writable_at(stamp(second)) == slot.writable_at(stamp(second))


def test_code_generation_does_not_follow_policy_revision(tmp_path):
    mesh = replica(tmp_path)
    slot, _ = edit(mesh, 20)
    slot, _ = edit(mesh, 25)
    assert slot.revision == 3 and slot.code_generation == 1
    event = AccessSlotEvent(
        "issuer",
        slot.slot_id,
        slot.logical_agent_id,
        4,
        "rotation",
        "AccessCodeRotated",
        code_tag=mesh.code_tag("issuer", "0001"),
    )
    mesh.apply_event(event, authenticated_peer_id="issuer")
    mesh.apply_event(event, authenticated_peer_id="issuer")
    assert mesh.slot("issuer", slot.slot_id).code_generation == 2


def test_old_wire_unchanged_and_new_window_is_explicit_extension(tmp_path):
    mesh = replica(tmp_path)
    event = AccessSlotEvent(
        "issuer", "slot_test", "agent_test", 2, "old_policy", "SlotPolicyChanged", policy=LONG
    )
    assert "policy_window" not in event.to_wire()
    slot, future = edit(mesh, 20)
    raw = future.to_wire()
    assert "policy_window" in raw
    assert AccessSlotEvent.from_wire(raw) == future
    with pytest.raises(AccessMeshError):
        AccessSlotEvent.from_wire({**raw, "kind": "SlotSuspended"})
    with pytest.raises(AccessMeshError):
        CapturedAccessWindow.from_wire({**raw["policy_window"], "unexpected": True})


def test_restart_keeps_capture_and_generation(tmp_path):
    mesh = replica(tmp_path)
    slot, _ = edit(mesh, 20)
    restarted = LocalAccessMesh(
        tmp_path / "replica.sqlite",
        local_node_id="issuer",
        trusted_issuers=frozenset({"issuer"}),
        proof_key=b"synthetic-proof-for-isolated-tests",
    )
    loaded = restarted.slot("issuer", slot.slot_id)
    assert loaded == slot
    assert_time(slot_cycle(loaded, stamp(75))["hard_expires_at"], 10875)
