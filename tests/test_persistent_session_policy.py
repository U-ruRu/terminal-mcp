"""Current Settings thresholds and admission policy; no runtime or durable stores."""

import pytest
from pydantic import ValidationError

from terminal_mcp.config import Settings


def _settings(**overrides):
    return Settings(
        _env_file=None,
        fleet_v1_source_enabled=False,
        fleet_v1_authority_enabled=False,
        fleet_v1_projection_enabled=False,
        fleet_v1_public_enabled=False,
        **overrides,
    )


def test_enabled_session_policy_preserves_configured_duration_and_rearm_default():
    settings = _settings(
        persistent_agents_enabled=True,
        persistent_session_duration_sec=120,
        persistent_session_warning_after_sec=60,
        persistent_session_alert_after_sec=90,
    )
    assert settings.persistent_session_duration_sec == 120
    assert settings.persistent_session_rearm_after_sec == 180


@pytest.mark.parametrize(
    "overrides,code",
    [
        (
            {
                "persistent_session_warning_after_sec": 120,
                "persistent_session_alert_after_sec": 110,
            },
            "warning_after_sec",
        ),
        ({"persistent_session_alert_after_sec": 60}, "alert_after_sec"),
        ({"persistent_session_rearm_after_sec": 0}, "rearm_after_sec"),
    ],
)
def test_enabled_session_policy_rejects_unordered_thresholds(overrides, code):
    values = dict(
        persistent_agents_enabled=True,
        persistent_session_duration_sec=120,
        persistent_session_warning_after_sec=60,
        persistent_session_alert_after_sec=90,
    )
    values.update(overrides)
    with pytest.raises(ValidationError, match=code):
        _settings(**values)
