"""Keep the pre-Architecture-A HTTP wire signatures during the internal cutover.

This test parses rather than imports transports, so it also runs in isolation
while independently implemented Application Core components are being assembled.
Intentional subsequent versioned surface changes must explicitly update the
fixture instead of silently changing existing clients' request contracts.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
BASELINE = json.loads(
    (Path(__file__).parent / "fixtures/http_architecture_a_contract.json").read_text()
)


def route_contract(source: str) -> list[dict]:
    result = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and isinstance(decorator.func.value, ast.Name)
            and decorator.func.value.id == "router"
            for decorator in node.decorator_list
        ):
            result.append(
                {
                    "name": node.name,
                    "arguments": ast.unparse(node.args),
                    "decorators": [ast.unparse(decorator) for decorator in node.decorator_list],
                }
            )
    return sorted(result, key=lambda item: json.dumps(item, sort_keys=True))


@pytest.mark.parametrize("filename", BASELINE["files"])
def test_http_compatibility_signatures_survive_application_cutover(filename):
    path = ROOT / "src/terminal_mcp/http" / filename
    assert route_contract(path.read_text()) == BASELINE["files"][filename]
