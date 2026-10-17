"""The checked-in current event catalogue matches its generating runtime."""

import json
from pathlib import Path

from terminal_mcp.mcp.event_store import event_catalog


def test_generated_events_catalog_matches_native_runtime():
    artifact = Path(__file__).resolve().parents[1] / "docs/contracts/mcp-events-list.json"
    assert json.loads(artifact.read_text()) == event_catalog()
