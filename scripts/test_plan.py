"""Conservative, inspectable test selection. Full release acceptance never selects."""

from __future__ import annotations

import ast
from collections import defaultdict, deque
from fnmatch import fnmatch
from pathlib import Path

DEV_TESTS = (
    "tests/test_public_errors.py",
    "tests/test_read_contract.py",
    "tests/test_work_window_domain.py",
    "tests/test_release_version.py",
    "tests/test_test_workflow.py",
    "tests/test_preparation.py",
)
COMPONENTS = {
    "auth": ("*auth*", "*oauth*", "*pairing*", "*identity*", "*provider*", "*bootstrap*"),
    "mesh": ("*mesh*", "*fleet*", "*remote*", "*access*", "*session_number*"),
    "storage": (
        "*storage*",
        "*sqlite*",
        "*migration*",
        "*restart*",
        "*recover*",
        "*retention*",
        "*durable*",
        "*lease*",
    ),
    "contracts": (
        "*contract*",
        "*planning*",
        "*bounds*",
        "*metadata*",
        "*public*",
        "*role*",
        "*mcp*",
        "*compact*",
    ),
    "commands": (
        "*command*",
        "*queue*",
        "*execution*",
        "*runtime*",
        "*terminal*",
        "*output*",
        "*read*",
        "*service*",
        "*fencing*",
        "*lease*",
    ),
    "slots": (
        "*slot*",
        "*persistent*",
        "*access*",
        "*window*",
        "*policy*",
        "*lifecycle*",
        "*android*",
        "*session*",
    ),
    "events": ("*event*", "*message*", "*mesh*", "*session*"),
    "deployment": (
        "*installer*",
        "*deployment*",
        "*topology*",
        "*release*",
        "*version*",
        "*privacy*",
    ),
}
# Explicit cross-component boundaries. A component run is feedback, not acceptance.
DEPENDENCIES = {
    "auth": ("storage", "contracts"),
    "mesh": ("auth", "storage", "contracts", "events"),
    "commands": ("storage", "contracts"),
    "slots": ("auth", "mesh", "storage", "events"),
    "events": ("storage", "contracts"),
    "deployment": ("storage",),
}
CRITICAL = (
    "src/terminal_mcp/auth/**",
    "src/terminal_mcp/storage/**",
    "src/terminal_mcp/fleet/**",
    "src/terminal_mcp/mcp/**",
    "src/terminal_mcp/application/**",
    "src/terminal_mcp/http/**",
    "src/terminal_mcp/core/**",
    "src/terminal_mcp/terminal/**",
    "src/terminal_mcp/deployment/**",
    "scripts/**",
    "deploy/**",
    "tests/conftest.py",
    "tests/helpers/**",
    "tests/fixtures/**",
    "tests/test_support/**",
    "docs/generated/**",
    "contracts/**",
    "apps/**",
    "pyproject.toml",
    "*requirements*",
    "*lock*",
    ".github/**",
)


def test_files(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in (root / "tests").rglob("test_*.py"))


def component_plan(root: Path, names: list[str]) -> dict:
    if not names or any(n not in COMPONENTS for n in names):
        raise ValueError("Choose one or more components: " + ", ".join(COMPONENTS))
    expanded = set(names)
    todo = list(names)
    while todo:
        for dependency in DEPENDENCIES.get(todo.pop(), ()):
            if dependency not in expanded:
                expanded.add(dependency)
                todo.append(dependency)
    patterns = [p for n in expanded for p in COMPONENTS[n]]
    selected = [p for p in test_files(root) if any(fnmatch(Path(p).stem, q) for q in patterns)]
    selected = sorted(set(selected) | {p for p in DEV_TESTS if (root / p).is_file()})
    if not selected:
        raise ValueError("The requested components selected no tests")
    return {
        "scope": "component",
        "tests": selected,
        "components": sorted(expanded),
        "reasons": [
            "Explicit component patterns and transitive dependencies in scripts/test_plan.py"
        ],
    }


def reverse_test_imports(root: Path) -> dict[str, set[str]]:
    """Resolve test-helper imports; application changes intentionally use full coverage."""
    modules: dict[str, str] = {}
    paths = list((root / "tests").rglob("*.py"))
    for path in paths:
        relative = path.relative_to(root)
        module = ".".join(relative.with_suffix("").parts)
        modules[module] = str(relative)
        modules[module.removeprefix("tests.")] = str(relative)
    reverse: dict[str, set[str]] = defaultdict(set)
    for path in paths:
        relative = str(path.relative_to(root))
        tree = ast.parse(path.read_text(), filename=relative)
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                prefix = node.module or ""
                names = [prefix, *(prefix + "." + alias.name for alias in node.names)]
                if node.level:
                    # Unresolved package-relative imports cannot silently narrow selection.
                    raise ValueError("Relative test-helper import requires full coverage")
            for name in names:
                if name in modules:
                    reverse[modules[name]].add(relative)
    return reverse


def affected_plan(root: Path, changed: list[str]) -> dict:
    all_tests = test_files(root)
    dev = [p for p in DEV_TESTS if (root / p).is_file()]
    reasons: list[str] = []
    for path in changed:
        if any(fnmatch(path, pattern) for pattern in CRITICAL):
            reasons.append(f"{path}: shared/critical input requires all integration tests")
        elif path.startswith("src/"):
            reasons.append(f"{path}: application dependency impact is not proven local")
        elif path not in all_tests:
            reasons.append(
                f"{path}: unknown, deleted, or non-test input; conservative full expansion"
            )
    if reasons:
        return {"scope": "full", "tests": ["tests"], "reasons": reasons, "changed": changed}
    try:
        reverse = reverse_test_imports(root)
    except (SyntaxError, ValueError, UnicodeError) as exc:
        return {
            "scope": "full",
            "tests": ["tests"],
            "changed": changed,
            "reasons": [f"Test dependency graph uncertain: {type(exc).__name__}"],
        }
    selected = set(changed) | set(dev)
    todo = deque(changed)
    while todo:
        path = todo.popleft()
        for importer in sorted(reverse.get(path, ())):
            if importer not in selected:
                selected.add(importer)
                todo.append(importer)
                reasons.append(f"{path} -> {importer}: imports changed test helper")
    return {
        "scope": "affected",
        "tests": sorted(selected),
        "changed": changed,
        "reasons": reasons or ["Changed tests plus development checks; no runtime change"],
    }
