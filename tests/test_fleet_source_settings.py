"""Fleet source settings own default admission and explicit node identity/path selection."""

import pytest
from pydantic import ValidationError

from terminal_mcp.config import Settings


@pytest.fixture(autouse=True)
def isolate_source_environment(monkeypatch):
    for key in (
        "TERMINAL_MCP_FLEET_V1_SOURCE_ENABLED",
        "TERMINAL_MCP_FLEET_V1_AUTHORITY_ENABLED",
        "TERMINAL_MCP_FLEET_V1_PROJECTION_ENABLED",
        "TERMINAL_MCP_FLEET_V1_PUBLIC_ENABLED",
        "TERMINAL_MCP_FLEET_ID",
        "TERMINAL_MCP_FLEET_INSTANCE_ID",
        "TERMINAL_MCP_FLEET_NODE_ID",
        "TERMINAL_MCP_FLEET_SIGNING_PRIVATE_KEY",
        "TERMINAL_MCP_FLEET_PEERS_JSON",
        "TERMINAL_MCP_FLEET_NODE_META_PATH",
    ):
        monkeypatch.delenv(key, raising=False)


def test_source_defaults_are_disabled_with_local_meta_path(tmp_path):
    defaults = Settings(_env_file=None, database_path=tmp_path / "runtime.sqlite3")
    assert defaults.fleet_v1_source_enabled is False
    assert defaults.fleet_id == ""
    assert defaults.fleet_node_id == ""
    assert defaults.effective_fleet_node_meta_path() == tmp_path / "fleet-node-meta.sqlite3"


def test_enabled_source_requires_fleet_identity():
    with pytest.raises(ValidationError, match="fleet_id is required"):
        Settings(
            _env_file=None,
            fleet_v1_source_enabled=True,
            fleet_instance_id="node-a",
            fleet_signing_private_key="placeholder",
        )


def test_source_explicit_node_identity_and_meta_path_are_used(tmp_path):
    enabled = Settings(
        _env_file=None,
        database_path=tmp_path / "runtime.sqlite3",
        fleet_v1_source_enabled=True,
        fleet_id="fleet-a",
        fleet_instance_id="legacy-node-a",
        fleet_node_id="node-a",
        fleet_signing_private_key="placeholder",
        fleet_node_meta_path=tmp_path / "node-meta.sqlite3",
    )
    assert enabled.effective_fleet_node_id() == "node-a"
    assert enabled.effective_fleet_node_meta_path() == tmp_path / "node-meta.sqlite3"
