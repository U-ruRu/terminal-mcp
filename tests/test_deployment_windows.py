"""Current Windows release service rendering and contained executable paths."""

import xml.etree.ElementTree as ET
from pathlib import PureWindowsPath

import pytest

from terminal_mcp.deployment.windows import service_xml


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
