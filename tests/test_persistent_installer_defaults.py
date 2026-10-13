"""Installer/default-file declarations; no shell execution or installation."""

from pathlib import Path

import pytest


def _declarations():
    root = Path(__file__).resolve().parents[1]
    return (root / "deploy/install.sh").read_text(), (root / ".env.example").read_text()


@pytest.mark.parametrize(
    "key,value",
    [
        ("TERMINAL_MCP_PERSISTENT_AGENTS_ENABLED", "true"),
        ("TERMINAL_MCP_PERSISTENT_SESSION_DURATION_SEC", "1380"),
        ("TERMINAL_MCP_PERSISTENT_SESSION_WARNING_AFTER_SEC", "1200"),
        ("TERMINAL_MCP_PERSISTENT_SESSION_ALERT_AFTER_SEC", "1320"),
        ("TERMINAL_MCP_PERSISTENT_SESSION_REARM_AFTER_SEC", "180"),
    ],
)
def test_persistent_default_is_declared_consistently_for_install_and_example(key, value):
    script, env_example = _declarations()
    assert f'{key}="${{{key}:-{value}}}"' in script
    assert f'ensure_env {key} "${{{key}:-{value}}}"' in script
    assert f'{key}="{value}"' in env_example


def test_admission_override_is_preserved_as_an_explicit_operator_choice():
    script, env_example = _declarations()
    lines = [
        line
        for line in script.splitlines()
        if "TERMINAL_MCP_LEGACY_AGENT_ADMISSION_ENABLED" in line
    ]
    assert lines
    assert all(":-false" not in line for line in lines)
    assert any("ensure_env TERMINAL_MCP_LEGACY_AGENT_ADMISSION_ENABLED" in line for line in lines)
    assert '# TERMINAL_MCP_LEGACY_AGENT_ADMISSION_ENABLED="false"' in env_example
    assert not any(
        line.startswith("TERMINAL_MCP_LEGACY_AGENT_ADMISSION_ENABLED=")
        for line in env_example.splitlines()
    )
