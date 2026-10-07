"""Pure temporal domain model for durable slot budgets and work sessions.

A WorkWindow owns the deadline. A WorkSession never owns a fresh duration and
cannot renew a budget by ending and starting. Persistence/application services
are responsible for compare-and-swap, fencing and audit commits; this module has
no transport, provider, database, scheduler or wall-clock dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum


class WorkWindowError(ValueError):
    """Stable domain rejection; transport projections are defined separately."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _integer(value: int, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WorkWindowError("policy_invalid_integer")
    if value < (1 if positive else 0):
        raise WorkWindowError("policy_invalid_duration" if positive else "policy_invalid_threshold")
    return value


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise WorkWindowError("timestamp_timezone_required")
    return value.astimezone(UTC)


def _thresholds(warning: int, draining: int, rearm: int) -> None:
    _integer(warning)
    _integer(draining)
    _integer(rearm)
    if draining > warning:
        raise WorkWindowError("policy_invalid_threshold_order")


def _deadline(opened_at: datetime, duration: int) -> datetime:
    _integer(duration, positive=True)
    try:
        return opened_at + timedelta(seconds=duration)
    except (OverflowError, ValueError) as exc:
        raise WorkWindowError("policy_invalid_duration") from exc


@dataclass(frozen=True, slots=True)
class SlotSessionPolicy:
    """Defaults copied into the NEXT WorkWindow, never referenced by an open one."""

    default_duration_seconds: int = 1380
    warning_before_expiry_seconds: int = 180
    draining_before_expiry_seconds: int = 90
    rearm_after_seconds: int = 180

    def __post_init__(self) -> None:
        _integer(self.default_duration_seconds, positive=True)
        _thresholds(
            self.warning_before_expiry_seconds,
            self.draining_before_expiry_seconds,
            self.rearm_after_seconds,
        )

    @classmethod
    def from_legacy(
        cls,
        *,
        duration_seconds: int,
        warning_after_seconds: int,
        alert_after_seconds: int,
        rearm_after_seconds: int,
    ) -> SlotSessionPolicy:
        """Capture a legacy node's actual thresholds without moving its deadlines."""
        for value in (duration_seconds, warning_after_seconds, alert_after_seconds):
            _integer(value, positive=True)
        if not warning_after_seconds < alert_after_seconds < duration_seconds:
            raise WorkWindowError("policy_invalid_threshold_order")
        return cls(
            default_duration_seconds=duration_seconds,
            warning_before_expiry_seconds=duration_seconds - warning_after_seconds,
            draining_before_expiry_seconds=duration_seconds - alert_after_seconds,
            rearm_after_seconds=rearm_after_seconds,
        )


class WindowLifecycle(StrEnum):
    OPEN = "open"
    EXPIRED = "expired"
    COOLDOWN = "cooldown"


class WindowPhase(StrEnum):
    ACTIVE = "active"
    WARNING = "warning"
    DRAINING = "draining"
    EXPIRED = "expired"
    COOLDOWN = "cooldown"


@dataclass(frozen=True, slots=True)
class WorkWindow:
    work_window_id: str
    logical_agent_id: str
    opened_at: datetime
    initial_duration_seconds: int
    effective_duration_seconds: int
    warning_before_expiry_seconds: int
    draining_before_expiry_seconds: int
    rearm_after_seconds: int
    window_revision: int = 1
    lifecycle: WindowLifecycle = WindowLifecycle.OPEN
    expired_at: datetime | None = None
    expiry_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.work_window_id or not self.logical_agent_id:
            raise WorkWindowError("window_identity_required")
        object.__setattr__(self, "opened_at", _utc(self.opened_at))
        _integer(self.initial_duration_seconds, positive=True)
        _integer(self.effective_duration_seconds, positive=True)
        _integer(self.window_revision, positive=True)
        _thresholds(
            self.warning_before_expiry_seconds,
            self.draining_before_expiry_seconds,
            self.rearm_after_seconds,
        )
        _deadline(self.opened_at, self.effective_duration_seconds)
        if not isinstance(self.lifecycle, WindowLifecycle):
            raise WorkWindowError("window_invalid_lifecycle")
        if self.expired_at is not None:
            object.__setattr__(self, "expired_at", _utc(self.expired_at))
            if self.expired_at < self.opened_at:
                raise WorkWindowError("window_invalid_expiry")
            if self.lifecycle is WindowLifecycle.OPEN:
                raise WorkWindowError("window_invalid_lifecycle")
        elif self.lifecycle is not WindowLifecycle.OPEN:
            raise WorkWindowError("window_expiry_required")

    @classmethod
    def open(
        cls,
        work_window_id: str,
        logical_agent_id: str,
        opened_at: datetime,
        policy: SlotSessionPolicy,
    ) -> WorkWindow:
        return cls(
            work_window_id=work_window_id,
            logical_agent_id=logical_agent_id,
            opened_at=opened_at,
            initial_duration_seconds=policy.default_duration_seconds,
            effective_duration_seconds=policy.default_duration_seconds,
            warning_before_expiry_seconds=policy.warning_before_expiry_seconds,
            draining_before_expiry_seconds=policy.draining_before_expiry_seconds,
            rearm_after_seconds=policy.rearm_after_seconds,
        )

    @property
    def hard_expires_at(self) -> datetime:
        return _deadline(self.opened_at, self.effective_duration_seconds)

    @property
    def rearm_at(self) -> datetime | None:
        if self.expired_at is None:
            return None
        try:
            return self.expired_at + timedelta(seconds=self.rearm_after_seconds)
        except (OverflowError, ValueError) as exc:
            raise WorkWindowError("policy_invalid_rearm") from exc

    def phase(self, now: datetime) -> WindowPhase:
        now = _utc(now)
        if self.lifecycle is WindowLifecycle.COOLDOWN:
            return WindowPhase.COOLDOWN
        if self.lifecycle is WindowLifecycle.EXPIRED or now >= self.hard_expires_at:
            return WindowPhase.EXPIRED
        remaining = (self.hard_expires_at - now).total_seconds()
        if remaining <= self.draining_before_expiry_seconds:
            return WindowPhase.DRAINING
        if remaining <= self.warning_before_expiry_seconds:
            return WindowPhase.WARNING
        return WindowPhase.ACTIVE

    def remaining_seconds(self, now: datetime) -> int:
        if self.lifecycle is not WindowLifecycle.OPEN:
            return 0
        return max(0, int((self.hard_expires_at - _utc(now)).total_seconds()))

    def successor_allowed(self, now: datetime) -> bool:
        """Fencing must finish before the application enters COOLDOWN."""
        return (
            self.lifecycle is WindowLifecycle.COOLDOWN
            and self.rearm_at is not None
            and _utc(now) >= self.rearm_at
        )

    def _compare_revision(self, expected_revision: int) -> None:
        _integer(expected_revision, positive=True)
        if expected_revision != self.window_revision:
            raise WorkWindowError("revision_conflict")

    def change(
        self,
        *,
        now: datetime,
        expected_revision: int,
        duration_seconds: int | None = None,
        delta_seconds: int | None = None,
        warning_before_expiry_seconds: int | None = None,
        draining_before_expiry_seconds: int | None = None,
    ) -> WindowChange:
        self._compare_revision(expected_revision)
        now = _utc(now)
        if self.lifecycle is not WindowLifecycle.OPEN or now >= self.hard_expires_at:
            raise WorkWindowError("window_expired")
        if now < self.opened_at:
            raise WorkWindowError("window_clock_before_open")
        if duration_seconds is not None and delta_seconds is not None:
            raise WorkWindowError("window_duration_ambiguous")
        if all(
            value is None
            for value in (
                duration_seconds,
                delta_seconds,
                warning_before_expiry_seconds,
                draining_before_expiry_seconds,
            )
        ):
            raise WorkWindowError("window_update_empty")
        duration = self.effective_duration_seconds
        if duration_seconds is not None:
            duration = _integer(duration_seconds, positive=True)
        if delta_seconds is not None:
            if isinstance(delta_seconds, bool) or not isinstance(delta_seconds, int):
                raise WorkWindowError("policy_invalid_integer")
            duration += delta_seconds
        _integer(duration, positive=True)
        changed = replace(
            self,
            effective_duration_seconds=duration,
            warning_before_expiry_seconds=(
                self.warning_before_expiry_seconds
                if warning_before_expiry_seconds is None
                else warning_before_expiry_seconds
            ),
            draining_before_expiry_seconds=(
                self.draining_before_expiry_seconds
                if draining_before_expiry_seconds is None
                else draining_before_expiry_seconds
            ),
            window_revision=self.window_revision + 1,
        )
        if changed.hard_expires_at <= now:
            # The operator revokes authority NOW, not retroactively at the new
            # (possibly past) deadline. Cooldown cannot be consumed in the past.
            changed = replace(
                changed,
                lifecycle=WindowLifecycle.EXPIRED,
                expired_at=now,
                expiry_reason="operator_duration",
            )
        return WindowChange(self, changed, changed.phase(now))

    def expire(self, *, now: datetime, expected_revision: int) -> WorkWindow:
        self._compare_revision(expected_revision)
        now = _utc(now)
        if self.lifecycle is not WindowLifecycle.OPEN:
            return self
        if now < self.hard_expires_at:
            raise WorkWindowError("window_not_expired")
        # Delayed reconciliation must not add a fresh cooldown delay on restart.
        return replace(
            self,
            lifecycle=WindowLifecycle.EXPIRED,
            expired_at=self.hard_expires_at,
            expiry_reason="hard_duration",
            window_revision=self.window_revision + 1,
        )

    def begin_cooldown(self, *, expected_revision: int) -> WorkWindow:
        self._compare_revision(expected_revision)
        if self.lifecycle is WindowLifecycle.COOLDOWN:
            return self
        if self.lifecycle is not WindowLifecycle.EXPIRED:
            raise WorkWindowError("window_not_expired")
        return replace(
            self,
            lifecycle=WindowLifecycle.COOLDOWN,
            window_revision=self.window_revision + 1,
        )


@dataclass(frozen=True, slots=True)
class WindowChange:
    previous: WorkWindow
    current: WorkWindow
    state: WindowPhase


@dataclass(frozen=True, slots=True)
class WorkSessionBinding:
    """Immutable role/version provenance attached to the EXISTING work-session row."""

    work_session_id: str
    logical_agent_id: str
    work_window_id: str
    session_epoch: int
    role: str
    contract_version: int

    def __post_init__(self) -> None:
        if not self.work_session_id or not self.logical_agent_id or not self.work_window_id:
            raise WorkWindowError("session_identity_required")
        _integer(self.session_epoch, positive=True)
        _integer(self.contract_version, positive=True)
        if self.role not in {"legacy", "executor", "coordinator"}:
            raise WorkWindowError("session_role_invalid")

    def require_contract(self, role: str, contract_version: int) -> None:
        _integer(contract_version, positive=True)
        if role != self.role or contract_version != self.contract_version:
            raise WorkWindowError("session_contract_conflict")
