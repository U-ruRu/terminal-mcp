"""Read-view limits; live session policy belongs exclusively to native ACS."""

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class AgentPolicy:
    # Fixed historical interpretation; never used to authorize a native cycle.
    idle_ttl_seconds: int = 300
    intent_ttl_seconds: int = 180
    max_session_seconds: int = 1500
    command_preview_chars: int = 160
    history_default_minutes: int = 60
    max_active_agents: int = 8

    def __post_init__(self):
        invalid = [name for name, value in asdict(self).items() if value <= 0]
        if invalid:
            raise ValueError(f"agent view values must be positive: {', '.join(invalid)}")
