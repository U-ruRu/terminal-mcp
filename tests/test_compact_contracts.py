"""Independent offline semantic gates for structural MCP compact contracts."""

from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from terminal_mcp.mcp import compact_contracts as c

GENERATED = c.ARTIFACTS


@pytest.fixture(scope="module")
def compiled():
    return c.build()


def _valid(schema, data):
    return Draft202012Validator(schema).is_valid(data)


def _model_valid(model, data):
    try:
        model.model_validate(data)
        return True
    except ValidationError:
        return False


def test_task_list_valid_invalid_strict_parity(compiled):
    role = "executor"
    schema = compiled["strict"][role + ".task_list"]["inputSchema"]
    model = c.contract_models(role)["task_list"]
    for request, allowed in (
        ({}, True),
        ({"namespace": "core", "limit": 10}, True),
        ({"cursor": "opaque", "limit": 100}, True),
        ({"limit": 0}, False),
        ({"limit": 101}, False),
        ({"namespace": ""}, False),
        ({"limit": "10"}, False),
        ({"unrecognized": "x"}, False),
    ):
        assert _valid(schema, request) == _model_valid(model, request) == allowed, (role, request)


def test_message_conditional_requirements_and_runtime_only_rules(compiled):
    role = "executor"
    schema = compiled["strict"][role + ".message"]["inputSchema"]
    model = c.contract_models(role)["message"]
    fixtures = (
        ({"action": "send", "text": "hello"}, True),
        ({"action": "send"}, False),
        ({"action": "ack", "message_hash": "m1"}, True),
        ({"action": "ack"}, False),
        ({"action": "reply", "message_hash": "m1", "text": "answer"}, True),
        ({"action": "reply", "message_hash": "m1"}, False),
        ({"action": "history"}, True),
        ({"action": "recipients"}, True),
        ({"action": "invalid"}, False),
    )
    for args, expected in fixtures:
        assert _valid(schema, args) == _model_valid(model, args) == expected, (role, args)
    # Pydantic @model_validator constraints cannot be inferred from JSON Schema:
    # the source-linked explicit annotations must retain these cases.
    runtime_only = (
        {"action": "send", "text": None},
        {"action": "send", "text": "ok", "message_hash": "m1"},
        {"action": "ack", "message_hash": "m1", "text": "extra"},
        {"action": "read", "text": "extra"},
    )
    for args in runtime_only:
        assert not _model_valid(model, args)
    rules = " ".join(
        note["text"] for note in compiled["coverage"][role + ".message"]["runtime_annotations"]
    )
    assert "message_hash must be absent or null" in rules
    assert "text must be absent or null" in rules
    assert "scope" in rules


def test_coordinator_task_manage_conditions_and_operation_roster(compiled):
    role = "coordinator"
    schema = compiled["strict"][role + ".task_manage"]["inputSchema"]
    model = c.contract_models(role)["task_manage"]
    from terminal_mcp.mcp.roles import _TASK_MANAGE_MODELS

    for args, allowed in (
        ({"action": "create", "namespace": "N", "isolation_hint": "isolated"}, True),
        ({"action": "create", "namespace": "N"}, False),
        ({"action": "state", "namespace": "N", "task_id": "t", "state": "blocked"}, True),
        ({"action": "state", "namespace": "N", "task_id": "t"}, False),
        ({"action": "archive", "namespace": "N", "task_id": "t", "archive_note": "why"}, True),
        ({"action": "archive", "namespace": "N", "task_id": "t"}, False),
        (
            {
                "action": "relate",
                "namespace": "N",
                "task_id": "t",
                "relation_kind": "depends_on",
                "related_task_id": "u",
            },
            True,
        ),
        ({"action": "relate", "namespace": "N", "task_id": "t"}, False),
        ({"action": "claim", "namespace": "N", "task_id": "t"}, False),
    ):
        assert _valid(schema, args) == allowed, args
        if args["action"] in _TASK_MANAGE_MODELS:
            # The outer Pydantic boundary carries some rules in
            # json_schema_extra; the action-specific canonical validator
            # enforces them at runtime.
            canonical = {**args, "code": None}
            assert _model_valid(_TASK_MANAGE_MODELS[args["action"]], canonical) == allowed, args
        else:
            assert not _model_valid(model, args)
    assert len(schema.get("allOf", [])) >= 8
    notes = compiled["coverage"]["coordinator.task_manage"]["runtime_annotations"]
    assert any("expected_revision" in n["text"] for n in notes)
    assert len(compiled["coverage"]["coordinator.task_manage"]["action_models"]) == 9
    assert any("request_id" in n["text"] for n in notes)


def test_access_attachment_and_comment_requests(compiled):
    schema = compiled["strict"]["executor.session"]["inputSchema"]
    model = c.contract_models("executor")["session"]
    for args, allowed in (
        ({"session_number": "0001"}, True),
        ({"session_number": "1"}, False),
        ({"action": "start", "session_number": "0001"}, False),
        ({"action": "attach", "session_number": "9999"}, True),
    ):
        assert _valid(schema, args) == _model_valid(model, args) == allowed
    model = c.contract_models("executor")["task_comment"]
    assert _model_valid(
        model, {"namespace": "n", "task_id": "t", "action": "comment", "comment_text": "x"}
    )
    assert not _model_valid(model, {"namespace": "n", "task_id": "t", "action": "comment"})
    assert _model_valid(
        model, {"namespace": "n", "task_id": "t", "action": "checkpoint", "checkpoint": "x"}
    )
    assert not _model_valid(model, {"namespace": "n", "task_id": "t", "action": "checkpoint"})


def test_schema_annotations_are_context_sensitive_and_unknown_fails_closed():
    x = {
        "type": "object",
        "description": "discard me",
        "properties": {
            "description": {"type": "string", "minLength": 1, "description": "meta"},
            "title": {"type": "string"},
            "nested": {"type": "object", "properties": {"example": {"type": "string"}}},
        },
        "required": ["description"],
    }
    stripped = c.strip_annotations(x)
    assert "description" not in stripped
    assert "description" in stripped["properties"] and "title" in stripped["properties"]
    assert "example" in stripped["properties"]["nested"]["properties"]
    r = c.SchemaRenderer({"$defs": {}}, "test")
    val = {"type": "string", "x-future-constraint": {"action": "must"}}
    rendered = r.render(val)
    assert (
        "SCHEMA_FALLBACK" in rendered and c.encoded(val["x-future-constraint"]).decode() in rendered
    )
    mixed = {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}
    assert r.render(mixed) == "string? (default=null)"
    assert r.fallbacks
    with pytest.raises(ValueError, match="Unresolved"):
        r.render({"$ref": "#/$defs/Missing"})


def test_proven_oneof_disjointness_not_inferred_for_overlaps():
    defs = {}
    branches = [
        {"type": "object", "properties": {"action": {"const": "send"}}, "required": ["action"]},
        {"type": "object", "properties": {"action": {"const": "ack"}}, "required": ["action"]},
    ]
    assert c.prove_disjoint(branches, defs)
    assert not c.prove_disjoint([{"type": "integer"}, {"type": "number"}], defs)
    assert not c.prove_disjoint(
        [
            {"type": "object", "properties": {"action": {"const": "send"}}},
            {"type": "object", "properties": {"action": {"const": "ack"}}},
        ],
        defs,
    )
    assert "ONE_OF" in c.SchemaRenderer({"$defs": {}}, "test").render(
        {"oneOf": [{"type": "integer"}, {"type": "number"}]}
    )


def test_artifact_stale_check_detects_a_modified_file(tmp_path):
    from scripts.build_compact_catalogs import check_artifacts

    expected = {"catalog-access.json": b"valid\n", "coverage.json": b"same\n"}
    for name, data in expected.items():
        (tmp_path / name).write_bytes(data)
    assert check_artifacts(tmp_path, expected) == []
    (tmp_path / "catalog-access.json").write_text("stale")
    assert check_artifacts(tmp_path, expected) == ["catalog-access.json"]


def test_builder_matches_application_actual_effective_catalogue(tmp_path, compiled):
    """Compare fixture-assembled real app servers, not only the isolated builders."""
    import asyncio

    from test_access_mesh_mcp_runtime import settings

    from terminal_mcp.app import create_app

    app = create_app(settings(tmp_path))
    for role in c.ROLE_NAMES:
        server = getattr(app.state, f"{role}_mcp")
        actual = [
            t.model_dump(mode="json", by_alias=True, exclude_none=True)
            for t in asyncio.run(server.list_tools())
        ]
        assert actual == compiled["after"][role]["tools"], role
