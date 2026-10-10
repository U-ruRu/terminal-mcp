"""Published docs, examples, packaged skill and runtime catalogs form one contract."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from zipfile import ZipFile

import pytest
from pydantic import TypeAdapter, ValidationError

from terminal_mcp.application.task_requests import TaskRequest
from terminal_mcp.http.access_mesh import OperatorMutation
from terminal_mcp.mcp.access_contracts import (
    AttachInput,
    IssuerSessionInput,
    MeshCommandReadInput,
    MeshMessageInput,
)
from terminal_mcp.mcp.role_contracts import TaskManageInput
from terminal_mcp.mcp.roles import COORDINATOR_TOOLS, EXECUTOR_TOOLS
from terminal_mcp.storage.sqlite import SCHEMA_VERSION
from terminal_mcp.version import __version__

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "skills/terminal-operations/references/tool-contract.md"
SKILL = ROOT / "skills/terminal-operations/SKILL.md"
CURRENT_DOCS = (
    ROOT / "README.md",
    ROOT / "docs/ARCHITECTURE.md",
    ROOT / "docs/connector-runtime-contract.md",
    REFERENCE,
)
DEPLOYMENT = ROOT / "docs/access-mesh-deployment.md"
EXAMPLE_PATTERN = re.compile(
    r"<!-- contract-example: ([\w.]+) -->\s*```json\s*(.*?)\s*```", re.DOTALL
)
EXAMPLES = [
    (index, kind, json.loads(payload))
    for index, (kind, payload) in enumerate(EXAMPLE_PATTERN.findall(REFERENCE.read_text()), 1)
]


def _tool_names(relative_path: str) -> tuple[str, ...]:
    tree = ast.parse((ROOT / relative_path).read_text())
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
            name = next((kw.value for kw in decorator.keywords if kw.arg == "name"), None)
            if isinstance(name, ast.Constant) and isinstance(name.value, str):
                names.append(name.value)
    return tuple(names)


def _catalog(text: str, prefix: str) -> tuple[str, ...]:
    lines = [line for line in text.splitlines() if line.startswith(prefix)]
    assert len(lines) == 1, prefix
    return tuple(re.findall(r"`([^`]+)`", lines[0]))


@pytest.mark.parametrize("path", (*CURRENT_DOCS, SKILL, DEPLOYMENT))
def test_current_docs_track_runtime_version(path: Path) -> None:
    assert __version__ in path.read_text(), path


def test_primary_docs_include_exact_public_contracts() -> None:
    legacy = _tool_names("src/terminal_mcp/mcp/server.py")
    access = _tool_names("src/terminal_mcp/mcp/access.py")
    assert legacy == ("session", "observe", "message", "task", "cmd", "context", "health")
    assert access == ("session",)
    assert len(EXECUTOR_TOOLS) == 10 and len(COORDINATOR_TOOLS) == 8
    manifest = json.loads(
        (ROOT / "src/terminal_mcp/mcp/access_mesh_schema_baselines_v2.json").read_text()
    )
    roles = {"access": access, "executor": EXECUTOR_TOOLS, "coordinator": COORDINATOR_TOOLS}
    for path in CURRENT_DOCS:
        text = path.read_text()
        assert _catalog(text, "Legacy compatibility catalog:") == legacy, path
        for role, expected in roles.items():
            assert _catalog(text, f"{role.title()} catalog:") == expected, path
            endpoint = f"/terminal-mcp/{role}/v1/mcp"
            assert endpoint in text, path
            assert manifest["roles"][role]["endpoint"] == endpoint
            assert tuple(tool["name"] for tool in manifest["roles"][role]["tools"]) == expected
        assert "attach-only" in text, path
        assert "permissive planning schemas" in text, path
        assert "Planned public contracts" not in text, path


@pytest.mark.parametrize("path", CURRENT_DOCS)
def test_current_docs_exclude_retired_public_tool_catalog(path: Path) -> None:
    text = path.read_text()
    for name in ("agent_start", "coordinate", "agent_finish"):
        assert f"`{name}`" not in text, (path, name)
    for obsolete in (
        "ready → in_progress on claim",
        "release returns in_progress to ready",
        "fixed one-role WorkSession",
        "sudo-audit.sh",
    ):
        assert obsolete not in text, (path, obsolete)


def test_packaged_terminal_operations_skill_matches_sources() -> None:
    expected = {
        "terminal-operations/SKILL.md": SKILL.read_bytes(),
        "terminal-operations/references/tool-contract.md": REFERENCE.read_bytes(),
    }
    with ZipFile(ROOT / "dist/terminal-operations.skill") as archive:
        assert set(archive.namelist()) == set(expected)
        assert len(archive.namelist()) == len(expected)
        assert archive.testzip() is None
        for name, content in expected.items():
            assert archive.read(name).replace(b"\r\n", b"\n") == content.replace(b"\r\n", b"\n")


@pytest.mark.parametrize(
    "index,kind,payload", EXAMPLES, ids=[f"example-{v[0]}-{v[1]}" for v in EXAMPLES]
)
def test_documented_json_examples_validate_in_authoritative_runtime_models(index, kind, payload):
    models = {
        "access.session": IssuerSessionInput,
        "role.session": AttachInput,
        "executor.command_read": MeshCommandReadInput,
        "role.message": MeshMessageInput,
        "coordinator.task_manage": TaskManageInput,
        "operator.mutate": OperatorMutation,
    }
    assert kind in models, (index, kind)
    models[kind].model_validate(payload)
    if kind == "coordinator.task_manage":
        TypeAdapter(TaskRequest).validate_python(payload)
    if kind == "role.session":
        assert payload.get("action", "attach") == "attach"
    if kind not in {"access.session", "role.session", "operator.mutate"}:
        assert {"code", "access_code", "logical_agent_id", "session_epoch"}.isdisjoint(payload)


def test_reference_has_examples_for_all_primary_boundaries():
    assert len(EXAMPLES) >= 16
    assert {kind for _, kind, _ in EXAMPLES} == {
        "access.session",
        "role.session",
        "executor.command_read",
        "role.message",
        "coordinator.task_manage",
        "operator.mutate",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "start", "access_code": "0427", "issuer_node_id": "firstbyte"},
        {"action": "end", "access_code": "0427", "issuer_node_id": "firstbyte"},
        {"action": "detach", "access_code": "0427", "issuer_node_id": "firstbyte"},
    ],
)
def test_role_lifecycle_remains_attach_only(payload):
    with pytest.raises(ValidationError):
        AttachInput.model_validate(payload)


def test_property_update_cannot_change_workflow_state():
    with pytest.raises(ValidationError):
        TypeAdapter(TaskRequest).validate_python(
            {
                "action": "update",
                "namespace": "example",
                "task_id": "sample",
                "state": "done",
            }
        )


def test_current_architecture_retains_state_and_execution_boundaries():
    text = (ROOT / "docs/ARCHITECTURE.md").read_text()
    assert f"runtime schema: **{SCHEMA_VERSION}**" in text
    for marker in (
        "ActorContext",
        "LogicalAgent",
        "WorkSession",
        "ExecutionPort",
        "observed_identity",
        "/run/terminal-mcp/executor.sock",
        "terminal-mcp.sqlite3",
        "auth.sqlite3",
        "fleet-control.sqlite3",
        "output.sqlite3",
        "release_on_end",
        "cleanup_pending",
        "transaction",
        "successor",
        "queued",
        "partial",
        "x-openai-isConsequential",
    ):
        assert marker in text, marker
    reference = REFERENCE.read_text()
    assert "command_read" in reference and "no hash" in reference
    assert "action=checkpoint" in reference and "action=comment" in reference


def test_deployment_guide_separates_source_tests_native_acceptance_and_security_rollback():
    text = DEPLOYMENT.read_text()
    for marker in (
        "FirstByte",
        "BacLOUD",
        "Secondary",
        "Main/Tokyo",
        "six",
        "immutable release SHA",
        "consistent SQLite",
        "rollback-excluded",
        "schema/security",
        "code-free",
        "First-contact messages",
        "Partition/restart",
        "Mobile/operator",
        "Legacy",
    ):
        assert marker in text, marker
    for role in ("access", "executor", "coordinator"):
        assert f"/terminal-mcp/{role}/v1/mcp" in text
    assert "install.sh stage` command" in text


@pytest.mark.parametrize("path", (*CURRENT_DOCS, SKILL, DEPLOYMENT))
def test_current_local_document_links_resolve(path: Path):
    for link in re.findall(r"(?<!!)\[[^\]]+\]\(([^)]+)\)", path.read_text()):
        if "://" in link or link.startswith("#"):
            continue
        target = (path.parent / link.split("#", 1)[0]).resolve()
        assert target.is_relative_to(ROOT), (path, link)
        assert target.exists(), (path, link)
