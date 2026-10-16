"""B1-B13 independent QA reproductions on the corrected offline contract.

These tests preserve Bravo-3's counterexamples and compare three distinct layers:
public transport (all object arguments forwarded), exact source Pydantic
validators, and generated assistant-facing rules plus source-linked coverage.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from terminal_mcp.application.task_requests import TaskUpdateRequest, task_request_to_backend
from terminal_mcp.mcp.access_contracts import MeshMessageInput
from terminal_mcp.mcp.roles import _TASK_MANAGE_MODELS

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / "docs" / "compact-contracts" / "generated"


def LOAD(name):
    return json.loads((FOLDER / name).read_text())


FUTURE = {
    role: {t["name"]: t for t in LOAD("catalog-" + role + ".json")["tools"]}
    for role in ("access", "executor", "coordinator")
}
SOURCE = LOAD("strict-contracts.json")
COVER = LOAD("coverage.json")
QA_ROWS = json.loads((ROOT / "docs/compact-contracts/qa-bravo3-action-matrix.json").read_text())[
    "rows"
]


def item(role, tool):
    return FUTURE[role][tool]


def text(role, tool):
    return item(role, tool)["description"]


def accepts(model, payload):
    try:
        model.model_validate(payload)
        return True
    except ValidationError:
        return False


def server_model(model, args):
    return accepts(model, {**args, "code": None})


def allows_transport(role, tool, args):
    return Draft202012Validator(item(role, tool)["inputSchema"]).is_valid(args)


@pytest.mark.parametrize("record", QA_ROWS, ids=lambda entry: entry["action"])
def test_B2_all_nine_allowed_forbidden_fields_match_canonical_source(record):
    name = record["action"]
    published = COVER["coordinator.task_manage"]["action_models"][name]
    model = _TASK_MANAGE_MODELS[name]
    strict = SOURCE["coordinator.task_manage"]["inputSchema"]
    expected_allowed = set(model.model_fields) & set(strict.get("properties", {}))
    expected_required = {
        field for field in expected_allowed if model.model_fields[field].is_required()
    }
    assert set(published["allowed"]) == expected_allowed
    assert set(published["forbidden"]) == (set(strict["properties"]) - expected_allowed)
    assert set(published["required_non_null"]) == expected_required
    assert published["closed"]
    assert f"{name}: allowed fields [" in text("coordinator", "task_manage")
    counterexample = record["forbidden_example"]
    assert allows_transport("coordinator", "task_manage", counterexample)
    assert not server_model(model, counterexample)


@pytest.mark.parametrize("role", ["executor", "coordinator"])
def test_B7_message_null_is_presence_sensitive_for_both_roles(role):
    d = text(role, "message")
    assert "explicit null is accepted" in d
    assert "non-null values forbidden" in d
    assert "scope=fleet" in d and "feature_disabled" in d
    assert "nullable mode defaults to notify" in d
    assert "forces final mode=alert" in d
    assert "input_validation_failed" in d and "path/reason/repair hint" in d
    assert "missing/null target means local broadcast" in d
    assert accepts(
        MeshMessageInput,
        {"action": "send", "text": "ok", "mode": "notify", "require_reply": True, "alert": False},
    )
    assert accepts(
        MeshMessageInput,
        {"action": "send", "text": "ok", "mode": None, "require_reply": False, "alert": False},
    )
    source = (ROOT / "src/terminal_mcp/application/access_mesh_messages.py").read_text()
    assert "if require_reply or alert:" in source
    assert 'normalized_mode = "alert"' in source

    for args in (
        {"action": "send", "text": "hello", "message_hash": None},
        {"action": "ack", "message_hash": "m", "text": None},
        {"action": "read", "text": None},
    ):
        assert allows_transport(role, "message", args)
        assert accepts(MeshMessageInput, args)
    for args in (
        {"action": "send", "text": None},
        {"action": "ack", "message_hash": "m", "text": "x"},
        {"action": "reply", "message_hash": "m", "text": None},
        {"action": "recipients", "message_hash": "m"},
    ):
        assert allows_transport(role, "message", args)
        assert not accepts(MeshMessageInput, args)


def test_B10_none_omits_update_but_empty_array_replaces():
    base = {
        "action": "update",
        "namespace": "qa",
        "task_id": "t",
        "expected_revision": 1,
        "code": None,
    }
    a = TaskUpdateRequest.model_validate({**base, "description": None})
    b = TaskUpdateRequest.model_validate({**base, "tags": []})
    _, _, _, first = task_request_to_backend(a)
    _, _, _, second = task_request_to_backend(b)
    assert "description" not in first
    assert second["tags"] == []
    desc = text("coordinator", "task_manage")
    assert "description:null is omitted/no change" in desc
    assert "tags:[] explicitly replaces" in desc
