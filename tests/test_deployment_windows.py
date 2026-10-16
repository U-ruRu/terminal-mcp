"""Pure Windows release rendering and durable rollback schema guard."""

import sqlite3
import xml.etree.ElementTree as ET
from pathlib import PureWindowsPath

import pytest

from terminal_mcp.deployment.windows import assert_rollback_compatible, schema, service_xml


def test_service_xml_preserves_quoted_paths_and_background_recovery():
    root = PureWindowsPath("C:/Program Data/TerminalMCP")
    python = root / "releases/abc/venv/Scripts/python.exe"
    service = ET.fromstring(service_xml(root, python))
    assert service.findtext("executable") == str(python)
    assert '"C:\\Program Data\\TerminalMCP\\config\\terminal-mcp.env"' in service.findtext(
        "arguments"
    )
    assert service.findtext("delayedAutoStart") == "true"
    assert service.findtext("hidewindow") == "true"
    assert service.find("onfailure").attrib == {"action": "restart", "delay": "10 sec"}
    assert service.find("serviceaccount") is None  # WinSW default is LocalSystem.


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


def test_code_rollback_checks_current_schema_without_restoring_data(tmp_path):
    import json

    data = tmp_path / "data"
    data.mkdir()
    path = data / "auth.sqlite3"
    with sqlite3.connect(path) as database:
        database.execute("CREATE TABLE identities (id TEXT)")
        database.execute("INSERT INTO identities VALUES ('original')")
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "schemas.json").write_text(json.dumps({path.name: schema(path)}))
    with sqlite3.connect(path) as database:
        database.execute("INSERT INTO identities VALUES ('later')")
    assert_rollback_compatible(tmp_path, backup)
    with sqlite3.connect(path) as database:
        assert database.execute("SELECT count(*) FROM identities").fetchone()[0] == 2
        database.execute("ALTER TABLE identities ADD COLUMN generation INTEGER")
    with pytest.raises(RuntimeError, match="matching durable database schemas"):
        assert_rollback_compatible(tmp_path, backup)
