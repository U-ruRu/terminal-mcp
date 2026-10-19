"""Current Windows release service rendering and contained executable paths."""

import xml.etree.ElementTree as ET
from pathlib import PureWindowsPath

import pytest

from terminal_mcp.deployment.windows import configure_browser_origins, service_xml


def test_service_xml_quotes_the_config_path():
    root = PureWindowsPath("C:/Program Data/TerminalMCP")
    python = root / "releases/abc/venv/Scripts/python.exe"
    service = ET.fromstring(service_xml(root, python))
    assert service.findtext("executable") == str(python)
    assert '"C:\\Program Data\\TerminalMCP\\config\\terminal-mcp.env"' in service.findtext(
        "arguments"
    )


@pytest.mark.parametrize(
    "root,python",
    [
        ("relative", "C:/releases/python.exe"),
        ("C:/TerminalMCP", "C:/outside/python.exe"),
        ("C:/Terminal%MCP", "C:/Terminal%MCP/releases/python.exe"),
        ("C:/TerminalMCP", "C:/TerminalMCP/releases/../python.exe"),
    ],
)
def test_service_xml_requires_contained_absolute_paths(root, python):
    with pytest.raises(ValueError):
        service_xml(PureWindowsPath(root), PureWindowsPath(python))


@pytest.mark.parametrize(
    "origins",
    [
        None,
        "https://trusted.example",
        "https://localhost",
        "https://localhost,https://console.example",
    ],
)
def test_installed_console_origins_preserve_operator_settings_and_converge(tmp_path, origins):
    from terminal_mcp.config import Settings

    env = tmp_path / "config/terminal-mcp.env"
    env.parent.mkdir()
    retained = (
        'TERMINAL_MCP_PUBLIC_BASE_URL="https://pc.example"\n'
        'TERMINAL_MCP_CONSOLE_PUBLIC_BASE_URL="https://console.example"\n'
        'TERMINAL_MCP_OAUTH_SIGNING_SECRET="fixture-only-original-key"\n'
    )
    env.write_text(
        retained + (f'TERMINAL_MCP_CONSOLE_ALLOWED_ORIGINS="{origins}"\n' if origins else "")
    )
    original = env.read_bytes()
    changed = configure_browser_origins(tmp_path)
    configured = Settings(_env_file=env)
    allowed = configured.browser_allowed_origins()
    assert {"https://localhost", "https://console.example", "https://pc.example"} <= set(allowed)
    if origins:
        assert set(origins.split(",")) <= set(allowed)
    assert "https://untrusted.example" not in allowed
    assert env.read_text().startswith(retained)
    assert configured.oauth_signing_secret == "fixture-only-original-key"
    backups = list((tmp_path / "backups").glob("console-origins-*.env"))
    assert len(backups) == int(changed)
    if changed:
        assert backups[0].read_bytes() == original
    before = env.read_bytes()
    assert configure_browser_origins(tmp_path) is False
    assert env.read_bytes() == before
