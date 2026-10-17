from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from terminal_mcp.core.work_windows import SlotSessionPolicy, WorkWindow, WorkWindowError

START = datetime(2026, 10, 6, 12, tzinfo=UTC)


def window(policy=None):
    return WorkWindow.open("ww-one", "agent-one", START, policy or SlotSessionPolicy())


def at(seconds):
    return START + timedelta(seconds=seconds)


@pytest.mark.parametrize(
    "elapsed,phase",
    [
        (1200, "warning"),
        (1290, "draining"),
        (1380, "expired"),
    ],
)
def test_exact_thresholds_use_current_deadline(elapsed, phase):
    assert window().phase(at(elapsed)) == phase


@pytest.mark.parametrize(
    "elapsed,delta,phase",
    [
        (1300, 600, "active"),
        (1000, -500, "expired"),
    ],
)
def test_signed_delta_recomputes_phase_without_sticky_warning(elapsed, delta, phase):
    mutation = window().change(now=at(elapsed), expected_revision=1, delta_seconds=delta)
    assert (mutation.current.effective_duration_seconds, mutation.state) == (1380 + delta, phase)


def test_total_duration_is_from_opening_not_from_mutation_time():
    change = window().change(now=at(1000), expected_revision=1, duration_seconds=1800)
    assert change.current.hard_expires_at == at(1800)
    assert change.current.remaining_seconds(at(1000)) == 800


def test_operator_shortening_anchors_cooldown_at_revocation_time():
    after = window().change(now=at(1000), expected_revision=1, duration_seconds=100).current

    assert after.expired_at == at(1000)
    assert after.rearm_at == at(1180)


def test_late_expiry_preserves_original_rearm_deadline():
    expired = window().expire(now=at(1800), expected_revision=1)

    assert expired.expired_at == at(1380)
    assert expired.rearm_at == at(1560)


def test_expired_window_cannot_be_extended():
    current = window().expire(now=at(1400), expected_revision=1)

    with pytest.raises(WorkWindowError, match="window_expired"):
        current.change(now=at(2000), expected_revision=2, delta_seconds=1200)


@pytest.mark.parametrize(
    "kwargs,code",
    [
        ({"duration_seconds": 100, "delta_seconds": 20}, "window_duration_ambiguous"),
        ({"warning_before_expiry_seconds": 10}, "policy_invalid_threshold_order"),
    ],
)
def test_invalid_mutations_do_not_change_window(kwargs, code):
    original = window()
    with pytest.raises(WorkWindowError, match=code):
        original.change(now=at(100), expected_revision=1, **kwargs)
    assert original.effective_duration_seconds == 1380
    assert original.window_revision == 1


def test_window_normalizes_timezone_to_utc():
    zoned = START.astimezone(timezone(timedelta(hours=4)))

    assert WorkWindow.open("ww", "agent", zoned, SlotSessionPolicy()).opened_at == START
