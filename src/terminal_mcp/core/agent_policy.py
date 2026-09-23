from dataclasses import dataclass

DEFAULT_SESSION_ALERT_MESSAGE = (
    "Ваша сессия закончилась. У пользователя для вас новая задача. "
    "Завершите сессию и немедленно вернитесь в чат к пользователю, "
    "чтобы дать ему промежуточный отчёт, получить дальнейшие указания и новую задачу."
)


@dataclass(frozen=True, slots=True)
class AgentPolicy:
    idle_ttl_seconds: int = 300
    intent_ttl_seconds: int = 180
    max_session_seconds: int = 1500
    session_warning_after_seconds: int = 1200
    session_alert_enabled: bool = True
    session_alert_after_seconds: int = 1380
    session_alert_repeat_seconds: int = 60
    session_alert_message: str = DEFAULT_SESSION_ALERT_MESSAGE
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
        if self.session_warning_after_seconds < 0:
            raise ValueError("session_warning_after_seconds must not be negative")
        if self.session_alert_enabled:
            if self.session_alert_after_seconds <= 0:
                raise ValueError(
                    "session_alert_after_seconds must be positive when alerts are enabled"
                )
            if self.session_alert_repeat_seconds <= 0:
                raise ValueError(
                    "session_alert_repeat_seconds must be positive when alerts are enabled"
                )
            if not self.session_alert_message.strip():
                raise ValueError("session_alert_message must not be empty when alerts are enabled")

    def as_dict(self):
        return {
            "idle_ttl_seconds": self.idle_ttl_seconds,
            "intent_ttl_seconds": self.intent_ttl_seconds,
            "max_session_seconds": self.max_session_seconds,
            "session_warning_after_seconds": self.session_warning_after_seconds,
            "session_alert_enabled": self.session_alert_enabled,
            "session_alert_after_seconds": self.session_alert_after_seconds,
            "session_alert_repeat_seconds": self.session_alert_repeat_seconds,
            "session_alert_message": self.session_alert_message,
            "event_window_seconds": self.event_window_seconds,
            "command_preview_chars": self.command_preview_chars,
            "history_default_minutes": self.history_default_minutes,
            "message_reminder_seconds": self.message_reminder_seconds,
            "message_reminder_calls": self.message_reminder_calls,
            "max_active_agents": self.max_active_agents,
        }
