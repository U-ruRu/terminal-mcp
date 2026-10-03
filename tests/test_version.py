import tomllib
from pathlib import Path

from terminal_mcp.version import __version__


def test_package_version_has_single_source():
    project = tomllib.loads(Path("pyproject.toml").read_text())
    major, minor, patch = __version__.split(".")
    assert all(part.isdigit() for part in (major, minor, patch))
    assert "version" not in project["project"]
    assert "version" in project["project"]["dynamic"]
    assert project["tool"]["hatch"]["version"]["path"] == "src/terminal_mcp/version.py"
