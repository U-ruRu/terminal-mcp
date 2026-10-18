"""Next-window policy changes keep active grants and peer clocks stable."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from terminal_mcp.core.access_mesh_grants import AccessSlotEvent, LocalAccessMesh, SlotPolicy
from terminal_mcp.core.access_policy_windows import (
    automatic_rearm_due,
    capture_window,
    latest_number_event,
    merged_slot_cycle,
    reopen_window,
    slot_cycle,
)

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
        slot.session.logical_agent_id if slot.session is not None else None,
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


def test_policy_capture_keeps_last_actual_window_until_start(tmp_path):
    mesh = replica(tmp_path)

    slot, _ = edit(mesh, 200)

    assert_time(slot_cycle(slot, stamp(200))["started_at"], 0)
    assert_time(slot_cycle(slot, stamp(200))["hard_expires_at"], 60)
    assert slot_cycle(slot, stamp(200))["state"] == "expired"


def test_policy_edit_preserves_captured_reopen_time(tmp_path):
    mesh = replica(tmp_path)

    slot, _ = edit(mesh, 65)

    assert_time(slot_cycle(slot, stamp(66))["rearm_at"], 75)


def test_idle_slot_does_not_start_as_side_effect_of_policy_edit(tmp_path):
    mesh = replica(tmp_path, start=None)
    slot, _ = edit(mesh, 20)
    assert slot_cycle(slot, stamp(20))["state"] == "idle"
    assert not slot.writable_at(stamp(100000))


def test_next_explicit_window_uses_updated_duration(tmp_path):
    mesh = replica(tmp_path)
    policy = replace(LONG, rearm_enabled=False)
    slot, _ = edit(mesh, 20, policy=policy)

    assert reopen_window(slot, stamp(75))["deadline"] == stamp(75 + policy.duration_seconds)


def test_deadline_override_retains_current_thresholds(tmp_path):
    mesh = replica(tmp_path)
    slot, _ = edit(mesh, 20)
    event = AccessSlotEvent(
        "issuer",
        slot.slot_id,
        slot.session.logical_agent_id if slot.session is not None else None,
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


@pytest.mark.parametrize("kind,rearm", [("temporary", False), ("persistent", True)])
@pytest.mark.parametrize("seconds,new_start", [(64, 0), (65, 65)])
def test_explicit_reopen_obeys_end_cooldown_boundary(tmp_path, kind, rearm, seconds, new_start):
    mesh = replica(tmp_path, policy=SlotPolicy(100, 60, rearm_enabled=rearm))
    slot = mesh.slot("issuer", "slot_test")
    ended = replace(slot, kind=kind, session=replace(slot.session, active=False, ended_at=stamp(5)))

    window = reopen_window(ended, stamp(seconds))

    assert window == {"start": stamp(new_start), "deadline": stamp(new_start + 100)}


@pytest.mark.parametrize("policy", [OLD, LONG])
def test_early_resume_preserves_end_marker_with_captured_policy(tmp_path, policy):
    mesh = replica(tmp_path)
    slot, _ = edit(mesh, 10, policy=policy)
    mesh.apply_event(
        AccessSlotEvent(
            "issuer", slot.slot_id, None, 3, "end", "SessionEnded", effective_at=stamp(20)
        ),
        authenticated_peer_id="issuer",
    )
    window = reopen_window(mesh.slot("issuer", slot.slot_id), stamp(25))

    mesh.apply_event(
        AccessSlotEvent(
            "issuer",
            slot.slot_id,
            "agent_test",
            4,
            "resume",
            "SessionStarted",
            effective_at=window["start"],
            deadline_at=window["deadline"],
            active_from=stamp(25),
        ),
        authenticated_peer_id="issuer",
    )

    resumed = mesh.slot("issuer", slot.slot_id)
    assert resumed.session.ended_at == stamp(20)
    assert resumed.session.active


@pytest.mark.parametrize("enabled", [False, True])
def test_automatic_rearm_uses_next_policy_flag_after_captured_cooldown(tmp_path, enabled):
    mesh = replica(tmp_path, policy=replace(OLD, rearm_enabled=not enabled))
    slot, _ = edit(mesh, 20, policy=replace(LONG, rearm_enabled=enabled))

    assert automatic_rearm_due(slot, stamp(75)) is enabled


def test_active_start_is_idempotent(tmp_path):
    mesh = replica(tmp_path)

    assert reopen_window(mesh.slot("issuer", "slot_test"), stamp(20)) is None


@pytest.mark.parametrize("end_seconds", [0, 5, -1])
def test_announced_end_closes_window_before_slot_event_delivery(tmp_path, end_seconds):
    slot = replica(tmp_path).slot("issuer", "slot_test")
    winner = {"started_at": T0.isoformat(), "hard_expires_at": stamp(60).isoformat()}
    ending = {
        "issuer_id": "issuer",
        "slot_id": slot.slot_id,
        "revision": 2,
        "event_id": "end",
        "ended_at": stamp(end_seconds).isoformat(),
        "cycle_key": f"issuer:{slot.slot_id}:{T0.isoformat()}",
    }

    cycle = merged_slot_cycle(slot, stamp(end_seconds), winner=winner, latest_end=ending)

    assert cycle["state"] == "cooldown"
    assert_time(cycle["rearm_at"], end_seconds + OLD.cooldown_seconds)


@pytest.mark.parametrize("start_seconds", [5, 4])
def test_announced_resume_follows_issuer_revision_when_clocks_tie_or_reverse(
    tmp_path, start_seconds
):
    slot = replica(tmp_path).slot("issuer", "slot_test")
    winner = {"started_at": T0.isoformat(), "hard_expires_at": stamp(60).isoformat()}
    ending = {
        "issuer_id": "issuer",
        "slot_id": slot.slot_id,
        "revision": 2,
        "event_id": "end",
        "ended_at": stamp(5).isoformat(),
        "cycle_key": f"issuer:{slot.slot_id}:{T0.isoformat()}",
    }
    starting = {
        "issuer_id": "issuer",
        "slot_id": slot.slot_id,
        "revision": 3,
        "event_id": "start",
        "active_from": stamp(start_seconds).isoformat(),
        "started_at": T0.isoformat(),
        "hard_expires_at": stamp(60).isoformat(),
    }

    cycle = merged_slot_cycle(
        slot, stamp(6), winner=winner, latest_start=starting, latest_end=ending
    )

    assert cycle["state"] == "active"
    assert_time(cycle["hard_expires_at"], 60)


def test_applied_end_survives_an_older_number_start_snapshot(tmp_path):
    slot = replica(tmp_path).slot("issuer", "slot_test")
    slot = replace(slot, revision=4, session=replace(slot.session, active=False, ended_at=stamp(6)))
    winner = {"started_at": T0.isoformat(), "hard_expires_at": stamp(60).isoformat()}
    starting = {
        "issuer_id": "issuer",
        "slot_id": slot.slot_id,
        "revision": 3,
        "event_id": "start",
        "active_from": stamp(5).isoformat(),
        "started_at": T0.isoformat(),
        "hard_expires_at": stamp(60).isoformat(),
    }

    assert (
        merged_slot_cycle(slot, stamp(7), winner=winner, latest_start=starting)["state"]
        == "cooldown"
    )


def test_number_event_order_uses_revision_before_a_reversed_clock():
    rows = [
        {
            "issuer_id": "issuer",
            "slot_id": "slot",
            "revision": revision,
            "at": stamp(seconds).isoformat(),
        }
        for revision, seconds in ((2, 5), (4, 4))
    ]

    assert latest_number_event(rows, "at")["revision"] == 4


def test_zero_cooldown_full_start_clears_end_marker_at_the_same_timestamp(tmp_path):
    mesh = replica(tmp_path, policy=replace(OLD, cooldown_seconds=0))
    mesh.apply_event(
        AccessSlotEvent("issuer", "slot_test", None, 2, "end", "SessionEnded", effective_at=T0),
        authenticated_peer_id="issuer",
    )

    mesh.apply_event(
        AccessSlotEvent(
            "issuer",
            "slot_test",
            "agent_test",
            3,
            "start",
            "SessionStarted",
            effective_at=T0,
            active_from=T0,
            deadline_at=stamp(60),
        ),
        authenticated_peer_id="issuer",
    )

    assert mesh.slot("issuer", "slot_test").session.ended_at is None
