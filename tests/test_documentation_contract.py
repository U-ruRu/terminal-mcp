"""Published docs, examples, packaged skill and runtime catalogs form one contract."""

from __future__ import annotations

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
    assert {kind for _, kind, _ in EXAMPLES} == {
        "access.session",
        "role.session",
        "executor.command_read",
        "role.message",
        "coordinator.task_manage",
        "operator.mutate",
    }


def test_role_lifecycle_remains_attach_only():
    with pytest.raises(ValidationError):
        AttachInput.model_validate(
            {"action": "start", "access_code": "0427", "issuer_node_id": "firstbyte"}
        )


@pytest.mark.parametrize("path", (*CURRENT_DOCS, SKILL, DEPLOYMENT))
def test_current_local_document_links_resolve(path: Path):
    for link in re.findall(r"(?<!!)\[[^\]]+\]\(([^)]+)\)", path.read_text()):
        if "://" in link or link.startswith("#"):
            continue
        target = (path.parent / link.split("#", 1)[0]).resolve()
        assert target.is_relative_to(ROOT), (path, link)
        assert target.exists(), (path, link)
