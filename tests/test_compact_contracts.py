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


def test_schema_annotation_removal_preserves_user_properties_with_annotation_names():
    schema = {
        "type": "object",
        "description": "Schema annotation",
        "properties": {
            "description": {"type": "string", "description": "Field annotation"},
            "title": {"type": "string"},
        },
        "required": ["description"],
    }

    assert c.strip_annotations(schema) == {
        "type": "object",
        "properties": {"description": {"type": "string"}, "title": {"type": "string"}},
        "required": ["description"],
    }


def test_overlapping_oneof_keeps_its_exclusive_constraint():
    rendered = c.SchemaRenderer({"$defs": {}}, "test").render(
        {"oneOf": [{"type": "integer"}, {"type": "number"}]}
    )

    assert "ONE_OF" in rendered


def test_artifact_stale_check_detects_a_modified_file(tmp_path):
    from scripts.build_compact_catalogs import check_artifacts

    expected = {"catalog-access.json": b"valid\n", "coverage.json": b"same\n"}
    for name, data in expected.items():
        (tmp_path / name).write_bytes(data)
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
