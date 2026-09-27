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

def generate_suffix() -> str:
    return "".join(secrets.choice(CROCKFORD) for _ in range(4))


def generate_agent_id() -> str:
    return f"{secrets.choice(NATO_WORDS)}-{generate_suffix()}"


def public_agent_name(agent_id: str | None) -> str:
    if not agent_id:
        return "anonymous"
    if agent_id == "anonymous":
        return agent_id
    name, separator, suffix = agent_id.rpartition("-")
    if separator and len(suffix) == 4 and all(ch in CROCKFORD for ch in suffix):
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
