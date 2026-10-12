"""Immutable current-window capture for changes to future Access slot defaults.

A policy edit is not a work-session extension. Replicas receive the same bounded
capture, so a delayed delivery cannot move the already-open deadline or warning
thresholds. After the captured window's own cooldown, the next window uses the
new defaults. An idle slot remains idle until an explicit start.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from terminal_mcp.core.access_mesh_grants import CapturedAccessWindow, SlotSnapshot


def _text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def effective_slot(slot: SlotSnapshot, now: datetime) -> SlotSnapshot:
    """Return the policy/anchor which actually gates the window at *now*."""
    capture = slot.policy_window
    if capture is None:
        return slot
    if capture.rearm_at is not None and now >= capture.rearm_at:
        return replace(slot, anchor=capture.rearm_at, deadline_at=None, policy_window=None)
    if capture.closed_at is not None and now >= capture.closed_at:
        return replace(
            slot,
            policy=capture.policy,
            anchor=capture.rearm_at,
            deadline_at=None,
            policy_window=None,
        )
    # A future anchor denotes the existing cooldown after an explicit end.
    anchor = capture.started_at
    if anchor is None and capture.rearm_at is not None:
        anchor = capture.rearm_at
    return replace(
        slot,
        policy=capture.policy,
        anchor=anchor,
        deadline_at=capture.hard_expires_at,
        policy_window=None,
    )


def slot_cycle(slot: SlotSnapshot, now: datetime) -> dict:
    """Canonical public phase calculation, shared by operator and runtime paths."""
    now = now.astimezone(UTC)
    slot = effective_slot(slot, now)
    base = {
        "state": slot.state,
        "started_at": None,
        "hard_expires_at": None,
        "remaining_seconds": 0,
        "rearm_at": None,
    }
    if slot.state != "active" or slot.anchor is None:
        return {**base, "state": slot.state if slot.state != "active" else "idle"}
    anchor = slot.anchor.astimezone(UTC)
    if now < anchor:
        return {**base, "state": "cooldown", "rearm_at": _text(anchor)}
    policy = slot.policy
    start = anchor
    end = slot.deadline_at or anchor + timedelta(seconds=policy.duration_seconds)
    if now >= end:
        if not policy.rearm_enabled:
            return {
                **base,
                "state": "expired",
                "started_at": _text(start),
                "hard_expires_at": _text(end),
            }
        next_start = end + timedelta(seconds=policy.cooldown_seconds)
        if now < next_start:
            return {
                **base,
                "state": "cooldown",
                "started_at": _text(start),
                "hard_expires_at": _text(end),
                "rearm_at": _text(next_start),
            }
        period = policy.duration_seconds + policy.cooldown_seconds
        index = int((now - next_start).total_seconds() // period)
        start = next_start + timedelta(seconds=index * period)
        end = start + timedelta(seconds=policy.duration_seconds)
    base.update(started_at=_text(start), hard_expires_at=_text(end))
    if now >= end:
        return {
            **base,
            "state": "cooldown",
            "rearm_at": _text(end + timedelta(seconds=policy.cooldown_seconds)),
        }
    remaining = (end - now).total_seconds()
    phase = "active"
    if policy.draining_seconds and remaining <= policy.draining_seconds:
        phase = "draining"
    elif policy.warning_seconds and remaining <= policy.warning_seconds:
        phase = "warning"
    return {**base, "state": phase, "remaining_seconds": max(0, int(remaining))}


def capture_window(slot: SlotSnapshot, now: datetime) -> CapturedAccessWindow:
    """Capture the actual cycle rather than the original, possibly old anchor."""
    from terminal_mcp.core.access_mesh_grants import CapturedAccessWindow

    # Suspension blocks grants independently. Preserve its underlying schedule
    # so an edit does not unexpectedly start or reset the cycle when resumed.
    readable = replace(slot, state="active") if slot.state == "suspended" else slot
    cycle = slot_cycle(readable, now)
    policy = effective_slot(readable, now).policy
    start = datetime.fromisoformat(cycle["started_at"]) if cycle["started_at"] else None
    end = datetime.fromisoformat(cycle["hard_expires_at"]) if cycle["hard_expires_at"] else None
    rearm = datetime.fromisoformat(cycle["rearm_at"]) if cycle["rearm_at"] else None
    if rearm is None and end is not None and policy.rearm_enabled:
        rearm = end + timedelta(seconds=policy.cooldown_seconds)
    return CapturedAccessWindow(policy, start, end, rearm)
