from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime

NATO_WORDS = (
    "Alpha",
    "Bravo",
    "Charlie",
    "Delta",
    "Echo",
    "Foxtrot",
    "Golf",
    "Hotel",
    "India",
    "Juliett",
    "Kilo",
    "Lima",
    "Mike",
    "November",
    "Oscar",
    "Papa",
    "Quebec",
    "Romeo",
    "Sierra",
    "Tango",
    "Uniform",
    "Victor",
    "Whiskey",
    "X-ray",
    "Yankee",
    "Zulu",
)
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_text(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def session_expiry_reason(
    session: dict | None,
    *,
    now: datetime | None = None,
    idle_ttl_seconds: float,
    max_session_seconds: float,
) -> str | None:
    """Return the lifecycle expiry reason for an otherwise-active Agent Session."""
    if not session or session.get("state") != "active":
        return None
    current = now or utc_now()
    age = (current - parse_utc(session["registered_at"])).total_seconds()
    idle = (current - parse_utc(session["last_activity_at"])).total_seconds()
    global_expires_at = session.get("global_expires_at")
    if global_expires_at:
        if current >= parse_utc(global_expires_at):
            return "max_session_duration"
    elif age >= max_session_seconds:
        return "max_session_duration"
    if idle >= idle_ttl_seconds:
        return "idle_timeout"
    return None


def session_is_live(
    session: dict | None,
    *,
    now: datetime | None = None,
    idle_ttl_seconds: float,
    max_session_seconds: float,
) -> bool:
    """Canonical predicate for whether an Agent Session may project live ownership."""
    return bool(
        session
        and session.get("state") == "active"
        and session_expiry_reason(
            session,
            now=now,
            idle_ttl_seconds=idle_ttl_seconds,
            max_session_seconds=max_session_seconds,
        )
        is None
    )


async def live_task_claims(
    task_store,
    agent_store,
    namespace: str,
    task_id: str,
    *,
    idle_ttl_seconds: float,
    max_session_seconds: float,
    now: datetime | None = None,
) -> list[dict]:
    """Project persisted claims through current Agent Session liveness, preserving claim order."""
    claims = await task_store.active_claims(namespace, task_id)
    if agent_store is None:
        return claims
    current = now or utc_now()
    live = []
    for claim in claims:
        session = await agent_store.get_session(claim["agent_id"])
        if session_is_live(
            session,
            now=current,
            idle_ttl_seconds=idle_ttl_seconds,
            max_session_seconds=max_session_seconds,
        ):
            live.append(claim)
    return live


def short_time(value: str | None) -> str:
    if not value:
        return "--:--:--Z"
    return parse_utc(value).strftime("%H:%M:%S")



def relative_time(value: str | None, now: datetime | None = None) -> str:
    if not value:
        return "unknown"
    current = now or utc_now()
    seconds = max(0, int((current - parse_utc(value)).total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days == 1:
        return f"yesterday {parse_utc(value).strftime('%H:%M')}"
    return f"{days}d ago"

AGENT_SUFFIX_CHARS = 8
LEGACY_AGENT_SUFFIX_CHARS = 4


def generate_suffix() -> str:
    """Return a compact 40-bit Crockford-Base32 capability suffix."""
    return "".join(secrets.choice(CROCKFORD) for _ in range(AGENT_SUFFIX_CHARS))


def generate_agent_id() -> str:
    return f"{secrets.choice(NATO_WORDS)}-{generate_suffix()}"


def is_agent_id(value: str, *, allow_legacy: bool = False) -> bool:
    name, separator, suffix = value.rpartition("-")
    lengths = {AGENT_SUFFIX_CHARS}
    if allow_legacy:
        lengths.add(LEGACY_AGENT_SUFFIX_CHARS)
    return (
        bool(separator)
        and name in NATO_WORDS
        and len(suffix) in lengths
        and all(ch in CROCKFORD for ch in suffix)
    )


def validate_message_routing(*, target=None, namespace=None, task_id=None, alert=False):
    if (namespace is None) != (task_id is None):
        return "message.task: namespace and task_id must be provided together"
    if target is not None and namespace is not None:
        if target.casefold() == "broadcast":
            return "message.target: broadcast cannot be combined with a task target"
        return "message.target: choose either an agent target or a task target"
    if alert and target is None and namespace is None:
        return (
            "message.alert: ALERT requires an explicit destination: public agent target, "
            "target='broadcast', or namespace+task_id"
        )
    return None


def public_agent_name(agent_id: str | None) -> str:
    if not agent_id:
        return "anonymous"
    if agent_id == "anonymous":
        return agent_id
    name, separator, suffix = agent_id.rpartition("-")
    if (
        separator
        and name in NATO_WORDS
        and len(suffix) in {AGENT_SUFFIX_CHARS, LEGACY_AGENT_SUFFIX_CHARS}
        and all(ch in CROCKFORD for ch in suffix)
    ):
        return name
    return agent_id


def normalize_preview(command: str, limit: int = 100) -> str:
    return re.sub(r"\s+", " ", command).strip()[:limit]


def scopes_overlap(left: str, right: str) -> bool:
    left = left.strip().strip("/")
    right = right.strip().strip("/")
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def find_scope_overlaps(own_scopes: list[str], sessions: list[dict]) -> list[dict]:
    found = []
    for session in sessions:
        if any(scopes_overlap(a, b) for a in own_scopes for b in session.get("work_scope", [])):
            match = next(
                b for a in own_scopes for b in session.get("work_scope", []) if scopes_overlap(a, b)
            )
            name = session.get("name") or public_agent_name(session.get("agent_id"))
            found.append({"name": name, "scope": match})
    return found
