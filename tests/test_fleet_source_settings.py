"""Explicit current Fleet source node identity and metadata path."""

import pytest

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


def test_source_explicit_node_identity_and_meta_path_are_used(tmp_path):
    enabled = Settings(
        _env_file=None,
        database_path=tmp_path / "runtime.sqlite3",
        fleet_v1_source_enabled=True,
        fleet_id="fleet-a",
        fleet_node_id="node-a",
        fleet_node_meta_path=tmp_path / "node-meta.sqlite3",
    )
    assert enabled.effective_fleet_node_id() == "node-a"
    assert enabled.effective_fleet_node_meta_path() == tmp_path / "node-meta.sqlite3"
