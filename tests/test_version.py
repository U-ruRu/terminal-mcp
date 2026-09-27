import tomllib
from pathlib import Path

from terminal_mcp.version import __version__


def test_package_version_matches_pyproject():
    project = tomllib.loads(Path("pyproject.toml").read_text())
    assert __version__ == "0.10.0"
    assert project["project"]["version"] == __version__
