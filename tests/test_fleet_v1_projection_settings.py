from terminal_mcp.config import Settings


def base(tmp_path):
    return {
        "_env_file": None,
        "database_path": tmp_path / "runtime.sqlite3",
        "fleet_v1_source_enabled": True,
        "fleet_id": "fleet-a",
        "fleet_node_id": "node-a",
        "fleet_instance_id": "node-a",
        "fleet_signing_private_key": "placeholder",
        "persistent_agents_enabled": True,
        "fleet_v1_authority_enabled": True,
        "fleet_control_node_id": "node-a",
    }


def test_projection_topology_selects_the_local_owner(tmp_path):
    settings = Settings(
        **base(tmp_path),
        fleet_v1_projection_enabled=True,
        fleet_projection_owner_node_id="node-a",
        fleet_projection_follower_node_id="node-b",
    )
    assert settings.effective_fleet_projection_role() == "owner"
