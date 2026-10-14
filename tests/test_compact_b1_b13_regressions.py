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

from terminal_mcp.application.task_requests import (
    TaskArchiveRequest,
    TaskCheckpointRequest,
    TaskClaimRequest,
    TaskCreateRequest,
    TaskRelateRequest,
    TaskReleaseRequest,
    TaskReviewRequest,
    TaskStateRequest,
    TaskUpdateRequest,
    task_request_to_backend,
)
from terminal_mcp.mcp.access_contracts import MeshMessageInput, MeshTaskCommentInput
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


def test_B11_all_20_transport_schemas_are_nonblocking_but_server_strict_is_preserved():
    assert sum(len(tools) for tools in FUTURE.values()) == 20
    for role, tools in FUTURE.items():
        for name, entry in tools.items():
            public = entry["inputSchema"]
            exact = SOURCE[f"{role}.{name}"]["inputSchema"]
            assert public == {"type": "object"}
            assert entry["outputSchema"] == {"type": "object"}
            assert "Arguments" in entry["description"]
            assert "Returns" in entry["description"]
            assert "Errors" in entry["description"]
            Draft202012Validator.check_schema(public)
            Draft202012Validator.check_schema(exact)
            assert allows_transport(role, name, {"made_up_field": ["text", False]})
    assert allows_transport("executor", "command_run", {"command": 42})
    assert not Draft202012Validator(SOURCE["executor.command_run"]["inputSchema"]).is_valid(
        {"command": 42}
    )


def test_B1_update_expected_revision_required_non_null_and_described():
    d = text("coordinator", "task_manage")
    row = COVER["coordinator.task_manage"]["action_models"]["update"]
    assert "expected_revision" in row["required_non_null"]
    assert "update: allowed fields" in d
    assert "update requires non-null expected_revision" in d
    for args in (
        {"action": "update", "namespace": "qa", "task_id": "t", "title": "x"},
        {
            "action": "update",
            "namespace": "qa",
            "task_id": "t",
            "expected_revision": None,
            "title": "x",
        },
    ):
        assert allows_transport("coordinator", "task_manage", args)
        assert not server_model(TaskUpdateRequest, args)
    assert server_model(
        TaskUpdateRequest,
        {"action": "update", "namespace": "qa", "task_id": "t", "expected_revision": 1},
    )


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


def test_B3_archive_accepts_either_non_null_note_without_contradiction():
    d = text("coordinator", "task_manage")
    assert "archive needs archive_note." not in d
    assert "either non-null archive_note or non-null note" in d
    for name in ("archive_note", "note"):
        assert server_model(
            TaskArchiveRequest,
            {"action": "archive", "namespace": "qa", "task_id": "t", name: "reason"},
        )
    assert not server_model(
        TaskArchiveRequest,
        {
            "action": "archive",
            "namespace": "qa",
            "task_id": "t",
            "archive_note": None,
            "note": None,
        },
    )


def test_B4_release_reason_is_optional_even_with_live_claim():
    d = text("executor", "task_claim")
    assert "release_reason may be required" not in d
    assert "release_reason is optional even when an existing live claim" in d
    assert "released_by_owner" in d
    args = {"action": "release", "namespace": "qa", "task_id": "t"}
    assert server_model(TaskReleaseRequest, args)
    assert allows_transport("executor", "task_claim", args)


def test_B5_task_state_allows_admitted_nonowner_and_no_claim_requirement():
    d = text("executor", "task_state")
    assert "a task owned by the current LogicalAgent" not in d
    assert "any admitted agent" in d
    assert "including on a task claimed by someone else" in d
    assert COVER["executor.task_state"]["model"].endswith(".TaskStateInput")
    source = (ROOT / "src/terminal_mcp/storage/tasks.py").read_text()
    assert "any admitted caller" in source
    assert "regardless of claims" in source
    assert allows_transport(
        "executor", "task_state", {"namespace": "qa", "task_id": "someone_elses", "state": "qa"}
    )


def test_B6_claim_and_release_field_whitelists_and_nulls():
    cov = COVER["executor.task_claim"]["action_models"]
    assert "claim_intent" in cov["claim"]["required_non_null"]
    assert "release_reason" in cov["claim"]["forbidden"]
    assert "claim_intent" in cov["release"]["forbidden"]
    assert "release_reason" not in cov["release"]["required_non_null"]
    for model, args in (
        (
            TaskClaimRequest,
            {"action": "claim", "namespace": "qa", "task_id": "t", "claim_intent": None},
        ),
        (
            TaskClaimRequest,
            {
                "action": "claim",
                "namespace": "qa",
                "task_id": "t",
                "claim_intent": "x",
                "release_reason": "no",
            },
        ),
        (
            TaskReleaseRequest,
            {"action": "release", "namespace": "qa", "task_id": "t", "claim_intent": "x"},
        ),
    ):
        assert allows_transport("executor", "task_claim", args)
        assert not server_model(model, args)
    assert "required non-null [action, claim_intent" in text("executor", "task_claim")


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


def test_B8_comment_checkpoint_nullable_extra_and_expected_revision_rejected():
    d = text("executor", "task_comment")
    assert "optional expected_revision" not in d
    assert "checkpoint does not accept expected_revision" in d
    assert "rejects comment_text" in d and "rejects checkpoint" in d
    for args in (
        {"namespace": "qa", "task_id": "t", "comment_text": None},
        {"namespace": "qa", "task_id": "t", "comment_text": "x", "checkpoint": "other"},
        {
            "action": "checkpoint",
            "namespace": "qa",
            "task_id": "t",
            "checkpoint": "ok",
            "comment_text": "other",
        },
        {
            "action": "checkpoint",
            "namespace": "qa",
            "task_id": "t",
            "checkpoint": "ok",
            "expected_revision": 1,
        },
    ):
        assert allows_transport("executor", "task_comment", args)
        assert not accepts(MeshTaskCommentInput, args)
    assert (
        "action"
        not in COVER["executor.task_comment"]["action_models"]["comment"]["required_non_null"]
    )
    assert accepts(MeshTaskCommentInput, {"namespace": "qa", "task_id": "t", "comment_text": "ok"})
    assert COVER["executor.task_comment"]["action_models"]["checkpoint"]["allowed"] == [
        "action",
        "checkpoint",
        "namespace",
        "task_id",
    ]


@pytest.mark.parametrize(
    "model,args",
    [
        (
            TaskCheckpointRequest,
            {"action": "checkpoint", "namespace": "qa", "task_id": "t", "checkpoint": None},
        ),
        (TaskStateRequest, {"action": "state", "namespace": "qa", "task_id": "t", "state": None}),
        (
            TaskReviewRequest,
            {
                "action": "review",
                "namespace": "qa",
                "task_id": "t",
                "dimensions": None,
                "verdict": "NON_BLOCKING",
            },
        ),
        (
            TaskReviewRequest,
            {
                "action": "review",
                "namespace": "qa",
                "task_id": "t",
                "dimensions": ["A"],
                "verdict": None,
            },
        ),
        (
            TaskArchiveRequest,
            {
                "action": "archive",
                "namespace": "qa",
                "task_id": "t",
                "archive_note": None,
                "note": None,
            },
        ),
        (
            TaskCreateRequest,
            {
                "action": "create",
                "namespace": "qa",
                "isolation_hint": "isolated",
                "state": "done",
                "result": None,
            },
        ),
    ],
)
def test_B9_nonnull_runtime_requirements_expressed(model, args):
    d = text("coordinator", "task_manage")
    assert "non-null" in d
    assert allows_transport("coordinator", "task_manage", args)
    assert not server_model(model, args)
    assert "checkpoint requires non-null" in d
    assert "state requires non-null" in d
    assert "review requires non-null dimensions" in d


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


def test_B12_checkpoint_false_expected_revision_removed_at_source():
    source = (ROOT / "src/terminal_mcp/mcp/roles.py").read_text()
    assert "checkpoint with action='checkpoint' and optional expected_revision" not in source
    assert "checkpoint with action='checkpoint' and a checkpoint payload" in source
    assert "optional expected_revision" not in text("executor", "task_comment")


def test_B13_relation_kind_open_ended_and_review_of_runtime_gate():
    d = text("coordinator", "task_manage")
    assert "relation_kind is an open string(1..64), not an enum" in d
    for marker in (
        "review_task_required",
        "missing_parent_candidate",
        "ambiguous_review_parent",
        "candidate_conflict",
    ):
        assert marker in d
    assert "source lane must be review" in d
    assert "parent candidate_ref must be nonempty" in d
    assert "no different review_of parent" in d
    arbitrary = {
        "action": "relate",
        "namespace": "qa",
        "task_id": "a",
        "relation_kind": "custom_rel",
        "related_task_id": "b",
    }
    assert server_model(TaskRelateRequest, arbitrary)
    assert allows_transport("coordinator", "task_manage", arbitrary)
    strict = SOURCE["coordinator.task_manage"]["inputSchema"]
    assert "enum" not in strict["properties"]["relation_kind"]
    source = (ROOT / "src/terminal_mcp/storage/tasks.py").read_text()
    assert 'if relation_kind == "review_of":' in source
    for marker in (
        "review_task_required",
        "missing_parent_candidate",
        "ambiguous_review_parent",
        "candidate_conflict",
    ):
        assert marker in source


def test_all_13_ids_are_covered_by_independent_regressions():
    names = [name for name in globals() if name.startswith("test_B")]
    joined = " ".join(names)
    for index in range(1, 14):
        assert f"B{index}" in joined, index
