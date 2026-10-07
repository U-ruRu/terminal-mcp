from __future__ import annotations

import ast
import re
from pathlib import Path
from zipfile import ZipFile

from terminal_mcp.version import __version__

ROOT = Path(__file__).resolve().parents[1]
CURRENT_DOCS = (
    ROOT / "README.md",
    ROOT / "docs" / "ARCHITECTURE.md",
    ROOT / "skills" / "terminal-operations" / "references" / "tool-contract.md",
)
CATALOG_PREFIX = "Канонический MCP-каталог:"


def _mcp_tool_names() -> tuple[str, ...]:
    tree = ast.parse((ROOT / "src" / "terminal_mcp" / "mcp" / "server.py").read_text())
    names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            func = decorator.func
            if not (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "mcp"
                and func.attr == "tool"
            ):
                continue
            name_kw = next((kw for kw in decorator.keywords if kw.arg == "name"), None)
            if (
                name_kw
                and isinstance(name_kw.value, ast.Constant)
                and isinstance(name_kw.value.value, str)
            ):
                names.append(name_kw.value.value)
    return tuple(names)


def _documented_catalog(text: str) -> tuple[str, ...]:
    line = next(line for line in text.splitlines() if line.startswith(CATALOG_PREFIX))
    return tuple(re.findall(r"`([^`]+)`", line))


def test_current_docs_track_runtime_version() -> None:
    for path in CURRENT_DOCS:
        text = path.read_text()
        assert __version__ in text, path


def test_current_docs_track_exact_mcp_catalog() -> None:
    actual = _mcp_tool_names()
    assert actual == ("session", "observe", "message", "task", "cmd", "context", "health")
    for path in CURRENT_DOCS:
        assert _documented_catalog(path.read_text()) == actual, path


def test_current_docs_exclude_retired_public_tool_catalog() -> None:
    retired = ("agent_start", "coordinate", "agent_finish")
    for path in CURRENT_DOCS:
        text = path.read_text()
        for name in retired:
            assert f"`{name}`" not in text, (path, name)


def test_packaged_terminal_operations_skill_matches_sources() -> None:
    archive = ROOT / "dist" / "terminal-operations.skill"
    with ZipFile(archive) as zf:
        assert zf.read("terminal-operations/SKILL.md") == (
            ROOT / "skills" / "terminal-operations" / "SKILL.md"
        ).read_bytes()
        assert zf.read("terminal-operations/references/tool-contract.md") == (
            ROOT / "skills" / "terminal-operations" / "references" / "tool-contract.md"
        ).read_bytes()
