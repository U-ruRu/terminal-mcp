"""The checked-in current event catalogue matches its generating runtime."""

import json
from pathlib import Path

from terminal_mcp.fleet.event_mesh import mesh_event_contracts


def test_generated_mesh_wire_catalog_is_current():
    artifact = Path(__file__).resolve().parents[1] / "docs/contracts/mcp-mesh-events.json"
    assert json.loads(artifact.read_text()) == mesh_event_contracts()
