"""Offline clean-deploy boundaries; ordinary retention never owns this reset."""

import json

import pytest

from terminal_mcp.deployment.clean import inventory


def _state(tmp_path, values=None):
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    (config / "terminal-mcp.env").write_text(
        "".join(f"{key}={json.dumps(value)}\n" for key, value in (values or {}).items())
    )
    return inventory(
        root=tmp_path / "installation",
        config=config,
        data=tmp_path / "data",
        cache=tmp_path / "cache",
        backups=tmp_path / "backups",
    )


@pytest.mark.parametrize(
    "area", ["data", "cache", "backups", "config", "installation/deployments/old/backup"]
)
def test_clean_removes_managed_state(tmp_path, area):
    state = _state(tmp_path)
    target = tmp_path / area / "old-state"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("previous installation")
    state.apply()
    assert not target.exists()


@pytest.mark.parametrize("key", ["AUTH_DATABASE_PATH", "DATABASE_PATH"])
def test_clean_removes_custom_database_and_security_sidecars(tmp_path, key):
    database = tmp_path / "custom.sqlite3"
    state = _state(tmp_path, {"TERMINAL_MCP_" + key: str(database)})
    for suffix in ("", "-wal", ".access-key", ".identity-anchor"):
        database.with_name(database.name + suffix).write_text("old")
    identity = database.with_name("node-identity.json")
    if key == "DATABASE_PATH":
        identity.write_text("old installation identity")
    state.apply()
    assert not list(tmp_path.glob("custom.sqlite3*"))
    assert not identity.exists()


def test_clean_preserves_unmanaged_data(tmp_path):
    state = _state(tmp_path)
    unrelated = tmp_path / "project.txt"
    unrelated.write_text("application workspace outside managed state")
    state.apply()
    assert unrelated.read_text() == "application workspace outside managed state"


def test_clean_rejects_link_before_deleting_state(tmp_path):
    state = _state(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "data").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="linked path"):
        state.apply()
    assert (tmp_path / "config/terminal-mcp.env").exists()


def test_clean_retry_is_idempotent(tmp_path):
    state = _state(tmp_path)
    state.apply()
    state.apply()
    assert not (tmp_path / "config").exists()


def test_clean_rejects_source_inside_managed_state(tmp_path):
    state = _state(tmp_path)
    with pytest.raises(ValueError, match="active release or source"):
        state.apply(protected=(tmp_path / "data/source",))
    assert (tmp_path / "config/terminal-mcp.env").exists()


def test_fresh_install_rejects_retained_backups(tmp_path):
    state = _state(tmp_path)
    (tmp_path / "config/terminal-mcp.env").unlink()
    backup = tmp_path / "installation/deployments/old/backup/auth.sqlite3"
    backup.parent.mkdir(parents=True)
    backup.write_text("old auth")
    with pytest.raises(ValueError, match="existing managed state"):
        state.require_empty()


def test_clean_retains_selected_auth_files_in_managed_directories(tmp_path):
    from terminal_mcp.deployment.clean_auth import paths

    config = tmp_path / "config"
    credentials = config / "credentials"
    credentials.mkdir(parents=True)
    state = _state(
        tmp_path,
        {
            "TERMINAL_MCP_AUTH_DATABASE_PATH": str(tmp_path / "data/auth.sqlite3"),
            "TERMINAL_MCP_OAUTH_USERNAME_FILE": str(credentials / "oauth-username"),
            "TERMINAL_MCP_OAUTH_PASSWORD_FILE": str(credentials / "oauth-password"),
        },
    )
    data = tmp_path / "data"
    data.mkdir()
    for suffix in ("", "-wal", ".access-key", ".identity-anchor"):
        (data / ("auth.sqlite3" + suffix)).write_text("retained")
    for name in ("oauth-username", "oauth-password"):
        (credentials / name).write_text("retained")
    (data / "worker.sqlite3").write_text("disposable")
    state.apply(retain=paths(config, data)[-1])
    assert (data / "auth.sqlite3").read_text() == "retained"
    assert (data / "auth.sqlite3.identity-anchor").read_text() == "retained"
    assert (credentials / "oauth-password").read_text() == "retained"
    assert not (data / "worker.sqlite3").exists()


def test_merge_env_keeps_auth_values_not_runtime_overrides(tmp_path):
    from terminal_mcp.deployment.clean import _values
    from terminal_mcp.deployment.clean_auth import merge_env

    config = tmp_path / "config"
    config.mkdir()
    (config / "terminal-mcp.env").write_text(
        'TERMINAL_MCP_OAUTH_SIGNING_SECRET="keep"\nTERMINAL_MCP_QUEUE_WORKERS="9"\n'
    )
    (config / "runtime.env").write_text(
        'TERMINAL_MCP_OAUTH_REQUIRED_SCOPES="terminal:read terminal:execute"\n'
        'TERMINAL_MCP_MAX_ACTIVE_AGENTS="90"\n'
    )
    fresh = tmp_path / "fresh.env"
    fresh.write_text('TERMINAL_MCP_QUEUE_WORKERS="4"\n')
    fresh.chmod(0o600)
    merge_env(config, tmp_path / "data", fresh)
    values = _values(fresh)
    assert values["TERMINAL_MCP_OAUTH_SIGNING_SECRET"] == "keep"
    assert values["TERMINAL_MCP_OAUTH_REQUIRED_SCOPES"] == "terminal:read terminal:execute"
    assert values["TERMINAL_MCP_QUEUE_WORKERS"] == "4"
    assert "TERMINAL_MCP_MAX_ACTIVE_AGENTS" not in values


def test_reset_pairing_revokes_device_grants_only(tmp_path):
    import sqlite3

    from terminal_mcp.deployment.clean_auth import reset_pairing

    auth = tmp_path / "auth.sqlite3"
    with sqlite3.connect(auth) as db:
        db.executescript("""
            CREATE TABLE oauth_clients(client_id TEXT);
            CREATE TABLE oauth_codes(client_id TEXT);
            CREATE TABLE oauth_refresh_tokens(client_id TEXT);
            CREATE TABLE console_devices(client_id TEXT);
            CREATE TABLE console_pairings(secret_hash TEXT);
            CREATE TABLE console_pairing_audit(event_type TEXT);
            INSERT INTO oauth_clients VALUES ('chatgpt'), ('paired');
            INSERT INTO oauth_codes VALUES ('chatgpt'), ('paired');
            INSERT INTO oauth_refresh_tokens VALUES ('chatgpt'), ('paired');
            INSERT INTO console_devices VALUES ('paired');
            INSERT INTO console_pairings VALUES ('pending');
            INSERT INTO console_pairing_audit VALUES ('previous');
        """)
    reset_pairing(auth)
    with sqlite3.connect(auth) as db:
        for table in ("oauth_clients", "oauth_codes", "oauth_refresh_tokens"):
            assert db.execute(f"SELECT client_id FROM {table}").fetchall() == [("chatgpt",)]
        for table in ("console_devices", "console_pairings", "console_pairing_audit"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_connection_link_file_is_root_private(tmp_path):
    from terminal_mcp.deployment.clean_auth import save_link

    url = "https://terminal-console.example/connect#one-time-secret"
    save_link(tmp_path, url)
    output = tmp_path / "link-url.txt"
    assert output.read_text().strip() == url
    assert output.stat().st_mode & 0o777 == 0o600


def test_preflight_refuses_partial_auth_before_cleanup(tmp_path):
    from terminal_mcp.deployment.clean_auth import preflight

    config = tmp_path / "config"
    config.mkdir()
    auth = tmp_path / "auth.sqlite3"
    auth.write_bytes(b"not a complete auth database")
    auth.chmod(0o600)
    (config / "terminal-mcp.env").write_text(f'TERMINAL_MCP_AUTH_DATABASE_PATH="{auth}"\n')
    (config / "terminal-mcp.env").chmod(0o600)
    with pytest.raises(ValueError, match="sidecar is missing"):
        preflight(config, tmp_path / "data")
