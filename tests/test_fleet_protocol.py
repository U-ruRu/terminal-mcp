"""Current Fleet capability normalization."""

from terminal_mcp.fleet.protocol import validate_capabilities


def test_capabilities_normalize_duplicates():
    assert validate_capabilities(["fleet.source.v1", "fleet.source.v1"]) == ("fleet.source.v1",)
