import json
import math
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from terminal_mcp.platform_paths import CONFIG_DIR, ENV_FILE, IS_WINDOWS, PROGRAM_DATA


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TERMINAL_MCP_", env_file=".env", extra="ignore")
    host: str = "127.0.0.1"
    port: int = 8080
    public_base_url: str = "http://127.0.0.1:8080"
    console_public_base_url: str = "https://terminal-console.solvenger.app"
    console_allowed_origins: str = ""
    env_file_path: Path = ENV_FILE
    database_path: Path = (
        PROGRAM_DATA / "data/terminal-mcp.sqlite3"
        if IS_WINDOWS
        else Path("./data/terminal-mcp.sqlite3")
    )
    auth_database_path: Path = (
        PROGRAM_DATA / "data/auth.sqlite3" if IS_WINDOWS else Path("./data/auth.sqlite3")
    )
    output_cache_path: Path = (
        PROGRAM_DATA / "data/output.sqlite3" if IS_WINDOWS else Path("./data/output.sqlite3")
    )
    output_line_max_bytes: int = 4 * 1024 * 1024
    output_command_max_bytes: int = 8 * 1024 * 1024
    output_retention_target_bytes: int = 192 * 1024 * 1024
    output_retention_max_bytes: int = 256 * 1024 * 1024
    output_retention_max_rows: int = 1_000_000
    output_retention_prune_rows: int = 500_000
    execution_mode: Literal["in_process", "unix"] = "in_process"
    executor_socket_path: Path = Path("/run/terminal-mcp/executor.sock")
    executor_rpc_timeout_sec: float = 10.0
    shell: str = (
        str(
            Path(os.environ.get("SystemRoot", "C:/Windows"))
            / "System32/WindowsPowerShell/v1.0/powershell.exe"
        )
        if IS_WINDOWS
        else "/bin/bash"
    )
    cwd: Path = PROGRAM_DATA / "work" if IS_WINDOWS else Path("/")
    terminal_user: str = "current" if IS_WINDOWS else "root"
    health_command: str = ""
    auth_mode: str = "none"
    mcp_auth_mode: str = ""
    actions_auth_mode: str = ""
    bearer_tokens: str = ""
    bearer_credentials_json: str = "[]"
    oauth_users_json: str = "[]"
    oauth_username_file: Path | None = None
    oauth_password_file: Path | None = None
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
    runtime_config_path: Path = CONFIG_DIR / "runtime.env"
    log_path: Path = (
        PROGRAM_DATA / "logs/terminal-mcp.log"
        if IS_WINDOWS
        else Path("/var/log/terminal-mcp/terminal-mcp.log")
    )
    metrics_host: str = "127.0.0.1"
    metrics_port: int = 8081
    agent_command_preview_chars: int = 160
    agent_history_default_minutes: int = 60
    max_active_agents: int = 8
    fleet_instance_id: str = ""
    fleet_signing_private_key: str = ""
    fleet_peers_json: str = "[]"
    fleet_peer_transports_path: str = str(CONFIG_DIR / "fleet-peer-transports.json")
    mesh_vpn_state_dir: str = str(CONFIG_DIR / "mesh-vpn")
    fleet_replication_interval_sec: float = 5.0
    fleet_request_timeout_sec: float = 3.0
    fleet_v1_projection_enabled: bool = False
    fleet_id: str = ""
    fleet_node_id: str = ""
    fleet_node_meta_path: Path | None = None
    fleet_control_node_id: str = ""
    fleet_control_path: Path | None = None
    fleet_projection_path: Path | None = None
    fleet_projection_owner_node_id: str = ""
    fleet_projection_follower_node_id: str = ""
    queue_workers: int = 4
    queue_reconcile_sec: float = 1.0
    access_mesh_proof_key: str = ""
    access_mesh_peers_json: str = "[]"
    access_mesh_duration_sec: int = 1200
    access_mesh_cooldown_sec: int = 60
    access_mesh_warning_sec: int = 120
    access_mesh_draining_sec: int = 30

    persistent_session_duration_sec: int = 23 * 60
    persistent_session_warning_after_sec: int = 20 * 60
    persistent_session_alert_after_sec: int = 22 * 60
    persistent_session_rearm_after_sec: int = 3 * 60

    @model_validator(mode="after")
    def validate_access_mesh(self):
        from terminal_mcp.core.access_mesh_grants import SlotPolicy

        if self.access_mesh_proof_key and len(self.access_mesh_proof_key.encode()) < 32:
            raise ValueError("access_mesh_proof_key must contain at least 32 bytes")
        peers = json.loads(self.access_mesh_peers_json)
        if not isinstance(peers, list) or any(
            not isinstance(peer, str) or not peer for peer in peers
        ):
            raise ValueError("access_mesh_peers_json must be an explicit peer id array")
        if self.fleet_instance_id in peers or len(set(peers)) != len(peers):
            raise ValueError("access_mesh peers must be distinct remote nodes")
        configured = {peer.get("instance_id") for peer in json.loads(self.fleet_peers_json)}
        if not set(peers).issubset(configured):
            raise ValueError("access_mesh peers require authenticated Fleet configuration")
        SlotPolicy(
            self.access_mesh_duration_sec,
            self.access_mesh_cooldown_sec,
            True,
            self.access_mesh_warning_sec,
            self.access_mesh_draining_sec,
        )
        return self

    @model_validator(mode="after")
    def validate_execution_topology(self):
        timeout = self.executor_rpc_timeout_sec
        if not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise ValueError("executor_rpc_timeout_sec must be finite and in (0, 300]")
        if IS_WINDOWS:
            if self.execution_mode != "in_process":
                raise ValueError("Windows execution requires in_process mode")
            return self
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
    def validate_fleet_v1_projection(self):
        if not self.fleet_v1_projection_enabled:
            return self
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
            raise ValueError("local fleet node must be configured projection owner or follower")
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
