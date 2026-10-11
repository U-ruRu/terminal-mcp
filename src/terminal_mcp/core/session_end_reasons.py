"""Truthful public termination causes from the committed WorkSession boundary."""

from terminal_mcp.core.orchestration import parse_utc

_MANUAL = frozenset({"manual", "SessionEnded", "session_end", "operator_end"})
_EXPIRED = frozenset({"expired", "hard_duration", "max_session_duration", "legacy_expired"})
_DEADLINE_CANDIDATES = frozenset({"local_cycle_ended", "expired_session_number_recycled"})


def session_end_reason(
    reason: str | None, *, ended_at: str | None = None, hard_expires_at: str | None = None
) -> str:
    """Keep other causes intact; a cycle fence alone is not proof of timer expiry."""
    if reason in _MANUAL:
        return "manual"
    if reason in _EXPIRED:
        return "expired"
    if reason in _DEADLINE_CANDIDATES and ended_at and hard_expires_at:
        try:
            if parse_utc(ended_at) >= parse_utc(hard_expires_at):
                return "expired"
        except (TypeError, ValueError):
            pass
    return reason or "unknown"
