import json
import math
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TERMINAL_MCP_", env_file=".env", extra="ignore")
    host: str = "127.0.0.1"
    port: int = 8080
    public_base_url: str = "http://127.0.0.1:8080"
    console_public_base_url: str = "https://terminal-console.solvenger.app"
    console_allowed_origins: str = ""
    env_file_path: Path = Path("/etc/terminal-mcp/terminal-mcp.env")
    database_path: Path = Path("./data/terminal-mcp.sqlite3")
    auth_database_path: Path = Path("./data/auth.sqlite3")
    output_cache_path: Path = Path("./data/output.sqlite3")
    output_line_max_bytes: int = 4 * 1024 * 1024
    output_command_max_bytes: int = 8 * 1024 * 1024
    output_retention_target_bytes: int = 192 * 1024 * 1024
    output_retention_max_bytes: int = 256 * 1024 * 1024
    output_retention_max_rows: int = 1_000_000
    output_retention_prune_rows: int = 500_000
    execution_mode: Literal["in_process", "unix"] = "in_process"
    executor_socket_path: Path = Path("/run/terminal-mcp/executor.sock")
    executor_rpc_timeout_sec: float = 10.0
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
    oauth_access_ttl_sec: int = 30 * 24 * 60 * 60
    oauth_refresh_ttl_sec: int = 2592000
    oauth_code_ttl_sec: int = 300
    console_ws_ticket_ttl_sec: int = 30
    console_ws_heartbeat_sec: float = 15.0
    console_ws_auth_check_sec: float = 5.0
    console_ws_poll_sec: float = 0.25
    console_ws_batch_size: int = 100
    max_read_lines: int = 5000
    cancel_grace_sec: float = 2.0
    runtime_config_path: Path = Path("/etc/terminal-mcp/runtime.env")
    log_path: Path = Path("/var/log/terminal-mcp/terminal-mcp.log")
    metrics_host: str = "127.0.0.1"
    metrics_port: int = 8081
    agent_idle_ttl_sec: int = 300
    agent_intent_ttl_sec: int = 180
    agent_max_session_sec: int = 1500
    agent_session_warning_after_sec: int = 1200
    agent_session_alert_enabled: bool = True
    agent_session_alert_after_sec: int = 1380
    agent_session_alert_repeat_sec: int = 60
    agent_session_alert_message: str = (
        "Ваша сессия закончилась. У пользователя для вас новая задача. "
        "Завершите сессию и немедленно вернитесь в чат к пользователю, "
        "чтобы дать ему промежуточный отчёт, получить дальнейшие указания и новую задачу."
    )
    agent_event_window_sec: int = 180
    agent_command_preview_chars: int = 160
    agent_history_default_minutes: int = 60
    message_reminder_sec: int = 180
    message_reminder_calls: int = 5
    agent_post_finish_message_grace_sec: int = 300
    max_active_agents: int = 8
    fleet_instance_id: str = ""
    fleet_signing_private_key: str = ""
    fleet_peers_json: str = "[]"
    fleet_replication_interval_sec: float = 5.0
    fleet_request_timeout_sec: float = 3.0
    fleet_legacy_replication_enabled: bool = True
    fleet_v1_source_enabled: bool = False
    fleet_v1_authority_enabled: bool = False
    fleet_v1_projection_enabled: bool = False
    fleet_v1_public_enabled: bool = False
    fleet_id: str = ""
    fleet_node_id: str = ""
    fleet_node_meta_path: Path | None = None
    fleet_control_node_id: str = ""
    fleet_control_path: Path | None = None
    fleet_projection_path: Path | None = None
    fleet_projection_owner_node_id: str = ""
    fleet_projection_follower_node_id: str = ""
    fleet_permit_ttl_ms: int = 10_000
    queue_workers: int = 4
    queue_reconcile_sec: float = 1.0
    persistent_agents_enabled: bool = False
    persistent_session_duration_sec: int = 23 * 60
    persistent_session_warning_after_sec: int = 20 * 60
    persistent_session_alert_after_sec: int = 22 * 60
    persistent_session_rearm_after_sec: int = 3 * 60
    legacy_agent_admission_enabled: bool | None = None

    @model_validator(mode="after")
    def validate_execution_topology(self):
        timeout = self.executor_rpc_timeout_sec
        if not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise ValueError("executor_rpc_timeout_sec must be finite and in (0, 300]")
        path = str(self.executor_socket_path)
        if (
            not self.executor_socket_path.is_absolute()
            or ".." in self.executor_socket_path.parts
            or any(ord(char) < 32 for char in path)
            or len(path.encode("utf-8")) > 107
        ):
            raise ValueError("executor_socket_path must be an absolute local Unix socket path")
        return self

    @model_validator(mode="after")
    def validate_persistent_session_thresholds(self):
        if not self.persistent_agents_enabled:
            return self
        duration = self.persistent_session_duration_sec
        warning = self.persistent_session_warning_after_sec
        alert = self.persistent_session_alert_after_sec
        rearm = self.persistent_session_rearm_after_sec
        if duration <= 0:
            raise ValueError("persistent_session_duration_sec must be positive")
        if warning <= 0 or warning >= duration:
            raise ValueError(
                "persistent_session_warning_after_sec must be positive and below duration"
            )
        if alert <= warning or alert >= duration:
            raise ValueError(
                "persistent_session_alert_after_sec must be above warning and below duration"
            )
        if rearm <= 0:
            raise ValueError("persistent_session_rearm_after_sec must be positive")
        return self

    @model_validator(mode="after")
    def validate_fleet_v1_source(self):
        if not self.fleet_v1_source_enabled:
            return self
        if not self.fleet_id.strip():
            raise ValueError("fleet_id is required when fleet_v1_source_enabled is true")
        if not (self.fleet_node_id.strip() or self.fleet_instance_id.strip()):
            raise ValueError(
                "fleet_node_id or fleet_instance_id is required when "
                "fleet_v1_source_enabled is true"
            )
        if not self.fleet_instance_id.strip() or not self.fleet_signing_private_key.strip():
            raise ValueError(
                "fleet v1 source currently requires configured fleet peer authentication"
            )
        return self

    @model_validator(mode="after")
    def validate_fleet_v1_authority(self):
        if not self.fleet_v1_authority_enabled:
            return self
        if not self.persistent_agents_enabled:
            raise ValueError("fleet v1 authority requires persistent_agents_enabled")
        if not self.fleet_v1_source_enabled:
            raise ValueError("fleet v1 authority requires fleet_v1_source_enabled")
        if not self.fleet_control_node_id.strip():
            raise ValueError("fleet_control_node_id is required when fleet v1 authority is enabled")
        if self.fleet_permit_ttl_ms <= 0 or self.fleet_permit_ttl_ms > 60_000:
            raise ValueError("fleet_permit_ttl_ms must be between 1 and 60000")
        return self

    @model_validator(mode="after")
    def validate_fleet_v1_projection(self):
        if self.fleet_v1_public_enabled and not self.fleet_v1_projection_enabled:
            raise ValueError("fleet v1 public requires fleet_v1_projection_enabled")
        if not self.fleet_v1_projection_enabled:
            return self
        if not self.fleet_v1_source_enabled:
            raise ValueError("fleet v1 projection requires fleet_v1_source_enabled")
        if not self.fleet_v1_authority_enabled:
            raise ValueError("fleet v1 projection requires fleet_v1_authority_enabled")
        owner = self.fleet_projection_owner_node_id.strip()
        if not owner:
            raise ValueError(
                "fleet_projection_owner_node_id is required when projection is enabled"
            )
        local = self.effective_fleet_node_id()
        follower = self.fleet_projection_follower_node_id.strip()
        if not follower:
            raise ValueError(
                "fleet_projection_follower_node_id is required when projection is enabled"
            )
        if follower == owner:
            raise ValueError("projection owner and follower must differ")
        if local not in {owner, follower}:
            raise ValueError(
                "local fleet node must be configured projection owner or follower"
            )
        return self

    def effective_fleet_node_id(self) -> str:
        return self.fleet_node_id.strip() or self.fleet_instance_id.strip()

    def effective_fleet_node_meta_path(self) -> Path:
        return self.fleet_node_meta_path or self.database_path.with_name("fleet-node-meta.sqlite3")

    def effective_fleet_control_path(self) -> Path:
        return self.fleet_control_path or self.database_path.with_name("fleet-control.sqlite3")

    def effective_fleet_projection_path(self) -> Path:
        return self.fleet_projection_path or self.database_path.with_name(
            "fleet-projection.sqlite3"
        )

    def effective_fleet_projection_role(self) -> str:
        local = self.effective_fleet_node_id()
        if local == self.fleet_projection_owner_node_id.strip():
            return "owner"
        if local == self.fleet_projection_follower_node_id.strip():
            return "follower"
        raise ValueError("local node is not part of configured projection topology")

    def mode_for(self, interface: str) -> str:
        explicit = self.mcp_auth_mode if interface == "mcp" else self.actions_auth_mode
        return explicit or self.auth_mode

    def legacy_admission_allowed(self) -> bool:
        if self.legacy_agent_admission_enabled is not None:
            return bool(self.legacy_agent_admission_enabled)
        return not (self.persistent_agents_enabled and self.mode_for("mcp") != "none")

    def browser_allowed_origins(self) -> tuple[str, ...]:
        from terminal_mcp.http.browser_security import canonical_origin

        public = urlsplit(self.public_base_url)
        public_origin = canonical_origin(f"{public.scheme}://{public.netloc}")
        configured = [
            canonical_origin(value)
            for value in self.console_allowed_origins.split(",")
            if value.strip()
        ]
        return tuple(dict.fromkeys([public_origin, *configured]))

    @staticmethod
    def parse_json_list(value: str) -> list[dict]:
        try:
            parsed = json.loads(value or "[]")
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
