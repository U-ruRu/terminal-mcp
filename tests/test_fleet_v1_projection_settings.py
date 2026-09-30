import pytest
from pydantic import ValidationError

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
    }


def test_projection_and_public_flags_default_off(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "runtime.sqlite3")
    assert settings.fleet_v1_projection_enabled is False
    assert settings.fleet_v1_public_enabled is False
    assert settings.effective_fleet_projection_path() == tmp_path / "fleet-projection.sqlite3"


def test_projection_topology_validation_and_role(tmp_path):
    settings = Settings(
        **base(tmp_path),
        fleet_v1_projection_enabled=True,
        fleet_projection_owner_node_id="node-a",
        fleet_projection_follower_node_id="node-b",
    )
    assert settings.effective_fleet_projection_role() == "owner"

    with pytest.raises(ValidationError, match="fleet_projection_owner_node_id"):
        Settings(**base(tmp_path), fleet_v1_projection_enabled=True)

    with pytest.raises(ValidationError, match="public requires"):
        Settings(
            **base(tmp_path),
            fleet_v1_public_enabled=True,
        )
