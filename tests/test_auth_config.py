"""OAuth lifetime configuration values."""

from terminal_mcp.config import Settings


def test_oauth_access_ttl_default_is_30_days():
    assert (
        Settings(
            _env_file=None,
            fleet_v1_source_enabled=False,
            fleet_v1_authority_enabled=False,
            fleet_v1_projection_enabled=False,
            fleet_v1_public_enabled=False,
        ).oauth_access_ttl_sec
        == 30 * 24 * 60 * 60
    )
