import json
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TERMINAL_MCP_", env_file=".env", extra="ignore")
    host: str = "127.0.0.1"
    port: int = 8080
    public_base_url: str = "http://127.0.0.1:8080"
    env_file_path: Path = Path("/etc/terminal-mcp/terminal-mcp.env")
    database_path: Path = Path("./data/terminal-mcp.sqlite3")
    shell: str = "/bin/bash"
    cwd: Path = Path("/")
    terminal_user: str = "root"
    health_command: str = ""
    auth_mode: str = "none"
    mcp_auth_mode: str = ""
    actions_auth_mode: str = ""
    bearer_tokens: str = ""
    bearer_credentials_json: str = "[]"
    oauth_users_json: str = "[]"
    admin_username: str = "admin"
    admin_password: str = "change-me"
    admin_session_secret: str = "change-me-session-secret"
    oauth_issuer: str = ""
    oauth_audience: str = ""
    oauth_jwks_url: str = ""
    oauth_signing_secret: str = "change-me"
    oauth_required_scopes: str = "terminal:read terminal:execute"
    oauth_admin_username: str = "admin"
    oauth_admin_password: str = "change-me"
    oauth_access_ttl_sec: int = 900
    oauth_refresh_ttl_sec: int = 2592000
    oauth_code_ttl_sec: int = 300
    max_read_lines: int = 5000
    cancel_grace_sec: float = 2.0
    runtime_config_path: Path = Path("/etc/terminal-mcp/runtime.env")
    log_path: Path = Path("/var/log/terminal-mcp/terminal-mcp.log")
    metrics_host: str = "127.0.0.1"
    metrics_port: int = 8081
    agent_idle_ttl_sec: int = 300
    agent_intent_ttl_sec: int = 180
    agent_max_session_sec: int = 1500
    agent_session_warning_sec: int = 180
    agent_event_window_sec: int = 180
    agent_command_preview_chars: int = 160
    agent_history_default_minutes: int = 60
    message_reminder_sec: int = 180
    message_reminder_calls: int = 5
    max_active_agents: int = 8
    queue_workers: int = 4
    queue_reconcile_sec: float = 1.0

    def mode_for(self, interface: str) -> str:
        explicit = self.mcp_auth_mode if interface == "mcp" else self.actions_auth_mode
        return explicit or self.auth_mode

    @staticmethod
    def parse_json_list(value: str) -> list[dict]:
        try:
            parsed = json.loads(value or "[]")
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
