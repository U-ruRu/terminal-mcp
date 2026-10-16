"""Current Fleet protocol identifiers, version admission and capability normalization."""

import pytest

from terminal_mcp.fleet.protocol import (
    FLEET_PROTOCOL_MAJOR,
    FleetProtocolError,
    canonical_event_id,
    validate_capabilities,
    validate_protocol_major,
)


def test_canonical_event_id_is_deterministic():
    first = canonical_event_id("fleet-a", "node-a", "abc123", 7)
    second = canonical_event_id("fleet-a", "node-a", "abc123", 7)
    assert first == second


def test_protocol_major_admits_current_and_rejects_unsupported():
    assert validate_protocol_major(FLEET_PROTOCOL_MAJOR) == 1
    with pytest.raises(FleetProtocolError, match="unsupported"):
        validate_protocol_major(2)


def test_capabilities_normalize_duplicates_and_reject_invalid():
    assert validate_capabilities(["fleet.source.v1", "fleet.source.v1"]) == ("fleet.source.v1",)
    with pytest.raises(FleetProtocolError, match="invalid fleet capability"):
        validate_capabilities(["bad capability"])
