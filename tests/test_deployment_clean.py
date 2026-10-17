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


def test_clean_removes_custom_database_and_security_sidecars(tmp_path):
    database = tmp_path / "custom.sqlite3"
    state = _state(tmp_path, {"TERMINAL_MCP_AUTH_DATABASE_PATH": str(database)})
    for suffix in ("", "-wal", ".access-key", ".identity-anchor"):
        database.with_name(database.name + suffix).write_text("old")
    state.apply()
    assert not list(tmp_path.glob("custom.sqlite3*"))


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
