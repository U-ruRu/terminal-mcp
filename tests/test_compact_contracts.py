"""Independent offline semantic gates for structural MCP compact contracts."""

from __future__ import annotations

import hashlib
import json

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from terminal_mcp.mcp import compact_contracts as c

GENERATED = c.ARTIFACTS


@pytest.fixture(scope="module")
def compiled():
    return c.build()


def _load(name):
    return json.loads((GENERATED / name).read_text())


def _refs_resolved(schema):
    """JSON Schema metaschema allows dangling refs; explicitly reject them."""
    definitions = schema.get("$defs", {})

    def visit(value):
        if isinstance(value, dict):
            if "$ref" in value:
                assert c.ref_name(value["$ref"]) in definitions
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(schema)


def _valid(schema, data):
    return Draft202012Validator(schema).is_valid(data)


def _model_valid(model, data):
    try:
        model.model_validate(data)
        return True
    except ValidationError:
        return False


def test_all_19_exact_effective_tools_and_artifacts(compiled):
    expected_counts = {"access": 1, "executor": 10, "coordinator": 8}
    assert {role: len(compiled["after"][role]["tools"]) for role in c.ROLE_NAMES} == expected_counts
    assert sorted(compiled["coverage"]) == sorted(compiled["strict"])
    assert len(compiled["coverage"]) == 19
    for role in c.ROLE_NAMES:
        before = compiled["before"][role]["tools"]
        after = compiled["after"][role]["tools"]
        assert [t["name"] for t in before] == [t["name"] for t in after]
        assert _load("catalog-" + role + ".json") == compiled["after"][role]
        assert _load("before-" + role + ".json") == compiled["before"][role]
        for old, new in zip(before, after, strict=True):
            assert old["annotations"] == new["annotations"]
            assert old["outputSchema"] == new["outputSchema"]
            assert old["description"] in new["description"]
            assert new["inputSchema"].get("x-terminal-mcp-action-matrix") == old["inputSchema"].get(
                "x-terminal-mcp-action-matrix"
            )
    assert _load("strict-contracts.json") == compiled["strict"]
    assert _load("coverage.json") == compiled["coverage"]
    assert _load("provenance.json") == compiled["provenance"]


def test_strict_input_output_refs_and_digest_preserved(compiled):
    assert set(compiled["provenance"]["roles"]) == set(c.ROLE_NAMES)
    for role in c.ROLE_NAMES:
        models = c.contract_models(role)
        for tool in compiled["after"][role]["tools"]:
            key = role + "." + tool["name"]
            source = compiled["strict"][key]
            coverage = compiled["coverage"][key]
            assert source["inputSchema"] == models[tool["name"]].model_json_schema()
            assert (
                hashlib.sha256(c.encoded(source["inputSchema"])).hexdigest()
                == coverage["strict_input_sha256"]
            )
            assert (
                hashlib.sha256(c.encoded(source["outputSchema"])).hexdigest()
                == coverage["strict_output_sha256"]
            )
            for schema in (
                source["inputSchema"],
                source["outputSchema"],
                tool["inputSchema"],
                tool["outputSchema"],
            ):
                Draft202012Validator.check_schema(schema)
                _refs_resolved(schema)
            assert coverage["output_fallback"] == f"strict-contracts.json:{key}.outputSchema"
            assert coverage["baseline_description_preserved"]
            assert sorted(source["inputSchema"].get("required", [])) == coverage["required_fields"]
            assert set(source["inputSchema"].get("properties", {})) == set(coverage["input_fields"])


@pytest.mark.parametrize("role", ["executor", "coordinator"])
def test_task_list_valid_invalid_strict_parity(role, compiled):
    schema = next(
        t["inputSchema"] for t in compiled["after"][role]["tools"] if t["name"] == "task_list"
    )
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
    description = next(
        t["description"] for t in compiled["after"][role]["tools"] if t["name"] == "task_list"
    )
    assert "next_cursor" in description and "summary-only" in description


@pytest.mark.parametrize("role", ["executor", "coordinator"])
def test_message_conditional_requirements_and_runtime_only_rules(role, compiled):
    schema = next(
        t["inputSchema"] for t in compiled["after"][role]["tools"] if t["name"] == "message"
    )
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
    assert "forbids message_hash" in rules and "forbid text" in rules
    assert "scope" in rules


def test_coordinator_task_manage_conditions_and_operation_roster(compiled):
    role = "coordinator"
    schema = next(
        t["inputSchema"] for t in compiled["after"][role]["tools"] if t["name"] == "task_manage"
    )
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
    rendered = next(
        t["description"] for t in compiled["after"][role]["tools"] if t["name"] == "task_manage"
    )
    assert "archive_note" in rendered and "isolation_hint" in rendered
    notes = compiled["coverage"]["coordinator.task_manage"]["runtime_annotations"]
    assert any("TaskRequest" in n["text"] for n in notes)
    assert any("request_id" in n["text"] for n in notes)


def test_access_attachment_and_comment_requests(compiled):
    schema = next(
        t["inputSchema"] for t in compiled["after"]["executor"]["tools"] if t["name"] == "session"
    )
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
    assert "SCHEMA_FALLBACK" in r.render(mixed)
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


def test_runtime_annotations_source_anchors_and_stale_detection(tmp_path, monkeypatch):
    valid = c._annotations()
    assert valid
    for entries in valid.values():
        for note in entries:
            assert note["anchor"] in (c.ROOT / note["source"]).read_text()
    bad = tmp_path / "annotation.json"
    bad.write_text(
        json.dumps(
            {
                "executor.message": [
                    {
                        "source": "src/terminal_mcp/mcp/role_contracts.py",
                        "anchor": "DOES_NOT_EXIST_ANCHOR",
                        "text": "lost",
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(c, "ANNOTATIONS", bad)
    with pytest.raises(ValueError, match="stale"):
        c._annotations()


def test_metrics_full_tools_list_and_byte_arithmetic():
    report = _load("metrics.json")
    assert report["tokenizer"] == "tiktoken==0.12.0 / cl100k_base"
    for role in c.ROLE_NAMES:
        previous = _load("before-" + role + ".json")
        future = _load("catalog-" + role + ".json")
        rollup = report["roles"][role]
        assert len(c.encoded(previous)) == rollup["before"]["serialized_tools_list"]["utf8_bytes"]
        assert len(c.encoded(future)) == rollup["after"]["serialized_tools_list"]["utf8_bytes"]
        assert rollup["delta"]["utf8_bytes"]["delta"] == (
            len(c.encoded(future)) - len(c.encoded(previous))
        )
        assert set(rollup["per_tool_delta"]) == {v["name"] for v in future["tools"]}
    counts = report["three_catalogs_total"]["utf8_bytes"]
    assert counts["delta"] == counts["after"] - counts["before"]


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
        assert actual == compiled["before"][role]["tools"], role
