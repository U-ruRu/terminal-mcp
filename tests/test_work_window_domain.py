from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from terminal_mcp.core.work_windows import (
    SlotSessionPolicy,
    WindowLifecycle,
    WindowPhase,
    WorkSessionBinding,
    WorkWindow,
    WorkWindowError,
)

START = datetime(2026, 10, 6, 12, tzinfo=UTC)


def window(policy=None):
    return WorkWindow.open("ww-one", "agent-one", START, policy or SlotSessionPolicy())


def at(seconds):
    return START + timedelta(seconds=seconds)


@pytest.mark.parametrize(
    "elapsed,phase",
    [
        (0, "active"),
        (1199, "active"),
        (1200, "warning"),
        (1289, "warning"),
        (1290, "draining"),
        (1379, "draining"),
        (1380, "expired"),
        (1400, "expired"),
    ],
)
def test_exact_thresholds_use_current_deadline(elapsed, phase):
    assert window().phase(at(elapsed)) == phase


def test_slot_defaults_are_a_snapshot_not_a_shared_mutable_policy():
    original = SlotSessionPolicy()
    current = window(original)
    next_policy = replace(original, default_duration_seconds=2400)
    assert current.effective_duration_seconds == 1380
    assert (
        WorkWindow.open("ww-next", "agent-one", at(2000), next_policy).effective_duration_seconds
        == 2400
    )
    with pytest.raises(FrozenInstanceError):
        original.default_duration_seconds = 1


@pytest.mark.parametrize(
    "elapsed,delta,phase",
    [
        (1300, 60, "warning"),
        (1300, 600, "active"),
        (1210, 600, "active"),
        (1000, -230, "warning"),
        (1000, -300, "draining"),
        (1000, -500, "expired"),
    ],
)
def test_signed_delta_recomputes_phase_without_sticky_warning(elapsed, delta, phase):
    before = window()
    mutation = before.change(now=at(elapsed), expected_revision=1, delta_seconds=delta)
    after = mutation.current
    assert mutation.previous is before
    assert after.opened_at == before.opened_at
    assert after.hard_expires_at == before.hard_expires_at + timedelta(seconds=delta)
    assert after.initial_duration_seconds == 1380
    assert after.effective_duration_seconds == 1380 + delta
    assert after.window_revision == 2
    assert mutation.state == phase


def test_total_duration_is_from_opening_not_from_mutation_time():
    change = window().change(now=at(1000), expected_revision=1, duration_seconds=1800)
    assert change.current.hard_expires_at == at(1800)
    assert change.current.remaining_seconds(at(1000)) == 800


def test_operator_shortening_expires_now_and_cannot_backdate_cooldown():
    after = window().change(now=at(1000), expected_revision=1, duration_seconds=100).current
    assert after.phase(at(1000)) is WindowPhase.EXPIRED
    assert after.expired_at == at(1000)
    assert after.hard_expires_at == at(100)
    assert after.rearm_at == at(1180)
    assert after.remaining_seconds(at(1000)) == 0
    assert not after.successor_allowed(at(2000))  # fencing not yet complete
    cooled = after.begin_cooldown(expected_revision=2)
    assert not cooled.successor_allowed(at(1179))
    assert cooled.successor_allowed(at(1180))


def test_late_reconciliation_does_not_delay_rearm_after_restart():
    expired = window().expire(now=at(1800), expected_revision=1)
    assert expired.expired_at == at(1380)
    assert expired.rearm_at == at(1560)
    assert not expired.successor_allowed(at(1800))
    assert expired.begin_cooldown(expected_revision=2).successor_allowed(at(1800))


@pytest.mark.parametrize("elapsed,state", [(1380, "open"), (2000, "expired"), (2000, "cooldown")])
def test_expired_or_cooldown_window_never_resurrects(elapsed, state):
    current = window()
    if state != "open":
        current = current.expire(now=at(1400), expected_revision=1)
    if state == "cooldown":
        current = current.begin_cooldown(expected_revision=2)
    with pytest.raises(WorkWindowError, match="window_expired"):
        current.change(
            now=at(elapsed), expected_revision=current.window_revision, delta_seconds=1200
        )


def test_revision_conflict_precedes_mutation_and_preserves_original():
    original = window()
    changed = original.change(now=at(200), expected_revision=1, delta_seconds=600).current
    with pytest.raises(WorkWindowError, match="revision_conflict"):
        changed.change(now=at(200), expected_revision=1, delta_seconds=600)
    assert original.window_revision == 1
    assert changed.window_revision == 2
    assert changed.effective_duration_seconds == 1980


def test_threshold_only_mutation_and_roundtrip_phase():
    original = window()
    assert original.phase(at(1200)) is WindowPhase.WARNING
    changed = original.change(
        now=at(1200),
        expected_revision=1,
        warning_before_expiry_seconds=100,
        draining_before_expiry_seconds=50,
    ).current
    assert changed.phase(at(1200)) is WindowPhase.ACTIVE
    assert changed.hard_expires_at == original.hard_expires_at


@pytest.mark.parametrize(
    "kwargs,code",
    [
        ({}, "window_update_empty"),
        ({"duration_seconds": 100, "delta_seconds": 20}, "window_duration_ambiguous"),
        ({"duration_seconds": 0}, "policy_invalid_duration"),
        ({"delta_seconds": -1380}, "policy_invalid_duration"),
        ({"delta_seconds": True}, "policy_invalid_integer"),
        ({"duration_seconds": 1.5}, "policy_invalid_integer"),
        ({"warning_before_expiry_seconds": 10}, "policy_invalid_threshold_order"),
    ],
)
def test_invalid_mutations_do_not_change_window(kwargs, code):
    original = window()
    with pytest.raises(WorkWindowError, match=code):
        original.change(now=at(100), expected_revision=1, **kwargs)
    assert original.effective_duration_seconds == 1380
    assert original.window_revision == 1


@pytest.mark.parametrize(
    "field",
    [
        "default_duration_seconds",
        "warning_before_expiry_seconds",
        "draining_before_expiry_seconds",
        "rearm_after_seconds",
    ],
)
@pytest.mark.parametrize("value", [True, "1380", 2.5])
def test_policy_rejects_coercion(field, value):
    with pytest.raises(WorkWindowError, match="policy_invalid_integer"):
        SlotSessionPolicy(**{field: value})


def test_policy_legacy_conversion_captures_actual_thresholds():
    policy = SlotSessionPolicy.from_legacy(
        duration_seconds=1380,
        warning_after_seconds=1200,
        alert_after_seconds=1290,
        rearm_after_seconds=180,
    )
    assert policy == SlotSessionPolicy()


def test_timezones_normalize_and_naive_clock_is_rejected():
    zoned = START.astimezone(timezone(timedelta(hours=4)))
    assert WorkWindow.open("ww", "agent", zoned, SlotSessionPolicy()).opened_at == START
    with pytest.raises(WorkWindowError, match="timestamp_timezone_required"):
        window().phase(START.replace(tzinfo=None))


def test_work_sessions_share_window_without_mutating_budget():
    budget = window()
    one = WorkSessionBinding("ws-1", "agent-one", budget.work_window_id, 1, "executor", 1)
    two = WorkSessionBinding("ws-2", "agent-one", budget.work_window_id, 2, "executor", 1)
    assert one.work_window_id == two.work_window_id
    assert budget.opened_at == START
    assert budget.hard_expires_at == at(1380)
    one.require_contract("executor", 1)
    with pytest.raises(WorkWindowError, match="session_contract_conflict"):
        one.require_contract("coordinator", 1)
    with pytest.raises(WorkWindowError, match="session_contract_conflict"):
        one.require_contract("executor", 2)
    assert budget.lifecycle is WindowLifecycle.OPEN
