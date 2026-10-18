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
    return replace(
        slot,
        policy=capture.policy,
        anchor=capture.started_at,
        deadline_at=capture.hard_expires_at,
        policy_window=None,
    )


def reopen_window(slot: SlotSnapshot, now: datetime) -> dict | None:
    """Select the explicit start window; ``None`` means the active start is unchanged.

    Only the prior end and its cooldown choose whether the old deadline survives.
    Slot kind, admission defaults and automatic Rearm never gate an explicit start.
    An early resume retains its end marker until the first full new window.
    """
    now = now.astimezone(UTC)
    current = effective_slot(slot, now)
    session = slot.session
    start = current.anchor
    deadline = current.deadline_at or (
        start + timedelta(seconds=current.policy.duration_seconds) if start is not None else None
    )
    fresh = {"start": now, "deadline": now + timedelta(seconds=slot.policy.duration_seconds)}
    if session is None or start is None or deadline is None:
        return fresh
    ended_at = session.ended_at
    if ended_at is None and now >= deadline:
        ended_at = deadline
    if ended_at is not None:
        if now >= ended_at + timedelta(seconds=current.policy.cooldown_seconds):
            return fresh
        if session.active and now < deadline:
            return None
        return {"start": start, "deadline": deadline}
    return None if session.active else {"start": start, "deadline": deadline}


def automatic_rearm_due(slot: SlotSnapshot, now: datetime) -> bool:
    """An existing session may request the same start transition when its Rearm is due."""
    if slot.state != "active" or slot.session is None:
        return False
    current = effective_slot(slot, now)
    if not slot.policy.rearm_enabled or current.anchor is None:
        return False
    end = (
        slot.session.ended_at
        or current.deadline_at
        or (current.anchor + timedelta(seconds=current.policy.duration_seconds))
    )
    return now.astimezone(UTC) >= end + timedelta(seconds=current.policy.cooldown_seconds)


def latest_number_event(rows, timestamp_field: str):
    """Issuer revision orders one slot; independent slots retain their time ordering."""
    latest = {}
    for row in rows:
        key = (row["issuer_id"], row["slot_id"])
        if key not in latest or row["revision"] > latest[key]["revision"]:
            latest[key] = row
    return max(
        latest.values(),
        key=lambda row: (
            datetime.fromisoformat(row[timestamp_field]),
            row["issuer_id"],
            row["slot_id"],
            row["revision"],
        ),
        default=None,
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
    if slot.state != "active" or slot.anchor is None or slot.session is None:
        return {**base, "state": slot.state if slot.state != "active" else "idle"}
    anchor = slot.anchor.astimezone(UTC)
    policy = slot.policy
    start = anchor
    end = slot.deadline_at or anchor + timedelta(seconds=policy.duration_seconds)
    base.update(started_at=_text(start), hard_expires_at=_text(end))
    if not slot.session.active and slot.session.ended_at is not None:
        due = slot.session.ended_at + timedelta(seconds=policy.cooldown_seconds)
        return {**base, "state": "cooldown" if now < due else "expired", "rearm_at": _text(due)}
    if now < anchor and (slot.session.active_from is None or now < slot.session.active_from):
        return {**base, "state": "cooldown", "rearm_at": _text(anchor)}
    if now >= end:
        next_start = end + timedelta(seconds=policy.cooldown_seconds)
        return {
            **base,
            "state": "cooldown" if now < next_start else "expired",
            "rearm_at": _text(next_start),
        }
    remaining = (end - now).total_seconds()
    phase = "active"
    if policy.draining_seconds and remaining <= policy.draining_seconds:
        phase = "draining"
    elif policy.warning_seconds and remaining <= policy.warning_seconds:
        phase = "warning"
    return {**base, "state": phase, "remaining_seconds": max(0, int(remaining))}


def merged_slot_cycle(slot, now, *, winner=None, latest_start=None, latest_end=None) -> dict:
    """Project one explicit Mesh window from an immutable set of registry rows.

    Callers resolve the overlapping number group and its effective slot before
    invoking this pure function. Transactional admission can therefore use the
    same lifecycle calculation without opening another database connection.
    """
    if winner is None or slot.session is None:
        return slot_cycle(slot, now)
    current = effective_slot(slot, now)
    session = slot.session
    windows, transitions = [], []
    if current.anchor is not None:
        expiry = current.deadline_at or (
            datetime.fromisoformat(winner["hard_expires_at"])
            if current.anchor == datetime.fromisoformat(winner["started_at"])
            else current.anchor + timedelta(seconds=current.policy.duration_seconds)
        )
        local = {
            "issuer_id": slot.issuer_id,
            "slot_id": slot.slot_id,
            "revision": slot.revision,
            "active_from": _text(session.active_from or current.anchor),
            "started_at": _text(current.anchor),
            "hard_expires_at": _text(expiry),
        }
        windows.append(local)
        transitions.append(
            {
                **local,
                "active": session.active,
                "at": local["active_from"] if session.active else _text(session.ended_at or expiry),
            }
        )
    if latest_start is not None:
        windows.append(latest_start)
        transitions.append({**latest_start, "active": True, "at": latest_start["active_from"]})
    if latest_end is not None:
        transitions.append({**latest_end, "active": False, "at": latest_end["ended_at"]})
    window = latest_number_event(windows, "active_from")
    if window is None:
        return slot_cycle(slot, now)
    transition = latest_number_event(transitions, "at")
    start = datetime.fromisoformat(window["started_at"])
    expiry = datetime.fromisoformat(window["hard_expires_at"])
    active_from = datetime.fromisoformat(window["active_from"])
    policy = current.policy if current.anchor == start else slot.policy
    ended_at = session.ended_at if current.anchor == start else None
    if not transition["active"]:
        ended_at = datetime.fromisoformat(transition["at"])
    elif window is latest_start:
        # A preceding end remains the cooldown anchor only for an early
        # resumption of that same window. A full start clears the marker.
        if latest_end is not None:
            prior = latest_number_event(
                [
                    {**latest_end, "at": latest_end["ended_at"]},
                    {**latest_start, "at": latest_start["active_from"]},
                ],
                "at",
            )
            end_anchor = datetime.fromisoformat(latest_end["cycle_key"].split(":", 2)[2])
            if prior["event_id"] == latest_start["event_id"] and end_anchor == start:
                ended_at = datetime.fromisoformat(latest_end["ended_at"])
        if ended_at is not None and active_from >= ended_at + timedelta(
            seconds=policy.cooldown_seconds
        ):
            ended_at = None
    session = replace(
        session, active=transition["active"], active_from=active_from, ended_at=ended_at
    )
    effective = replace(
        slot,
        anchor=start,
        deadline_at=expiry,
        policy=policy,
        policy_window=None,
        session=session,
    )
    return slot_cycle(effective, now)


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
    if rearm is None and end is not None:
        rearm = end + timedelta(seconds=policy.cooldown_seconds)
    return CapturedAccessWindow(policy, start, end, rearm)
