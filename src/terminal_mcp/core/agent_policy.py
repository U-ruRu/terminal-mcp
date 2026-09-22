from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AgentPolicy:
    idle_ttl_seconds: int = 300
    intent_ttl_seconds: int = 180
    max_session_seconds: int = 1500
    session_warning_seconds: int = 180
    event_window_seconds: int = 180
    command_preview_chars: int = 160
    history_default_minutes: int = 60
    message_reminder_seconds: int = 180
    message_reminder_calls: int = 5
    max_active_agents: int = 8

    def __post_init__(self):
        positive = {
            "idle_ttl_seconds": self.idle_ttl_seconds,
            "intent_ttl_seconds": self.intent_ttl_seconds,
            "max_session_seconds": self.max_session_seconds,
            "session_warning_seconds": self.session_warning_seconds,
            "event_window_seconds": self.event_window_seconds,
            "command_preview_chars": self.command_preview_chars,
            "history_default_minutes": self.history_default_minutes,
            "message_reminder_seconds": self.message_reminder_seconds,
            "message_reminder_calls": self.message_reminder_calls,
            "max_active_agents": self.max_active_agents,
        }
        invalid = [name for name, value in positive.items() if value <= 0]
        if invalid:
            raise ValueError(f"agent policy values must be positive: {', '.join(invalid)}")
        if self.session_warning_seconds > self.max_session_seconds:
            raise ValueError("session_warning_seconds must not exceed max_session_seconds")
