"""Permissive task discovery with complete action-specific runtime annotations.

Planning exposes every field directly, without flattening away properties when
an action also has conditional constraints. JSON-Schema assertions live under
an annotation; runtime request models remain the sole validation boundary.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Collection

from pydantic import TypeAdapter

from terminal_mcp.application.task_requests import TaskRequest


def _shape_hint(schema: dict, definitions: dict) -> str:
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        return _shape_hint(definitions.get(name, {}), definitions)
    if "const" in schema:
        return json.dumps(schema["const"], ensure_ascii=False)
    if "enum" in schema:
        return " | ".join(json.dumps(value, ensure_ascii=False) for value in schema["enum"])
    choices = schema.get("anyOf") or schema.get("oneOf")
    if choices:
        return " | ".join(dict.fromkeys(_shape_hint(item, definitions) for item in choices))
    hint = schema.get("type", "JSON value")
    if hint == "array":
        hint += f" of ({_shape_hint(schema.get('items', {}), definitions)})"
    constraints = [
        f"{name}={json.dumps(schema[name], ensure_ascii=False)}"
        for name in (
            "minLength",
            "maxLength",
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
            "minItems",
            "maxItems",
            "uniqueItems",
            "pattern",
            "format",
        )
        if name in schema
    ]
    return "; ".join([hint, *constraints])


def task_action_planning_schema(
    *,
    actions: Collection[str] | None = None,
    exclude_fields: Collection[str] = (),
) -> dict:
    """Flat argument names for Coordinator; full strict schema remains discoverable."""
    runtime = TypeAdapter(TaskRequest).json_schema()
    mapping = runtime["discriminator"]["mapping"]
    selected = set(mapping) if actions is None else set(actions)
    unknown = selected - set(mapping)
    if unknown:
        raise ValueError(f"Unknown task planning actions: {sorted(unknown)}")
    mapping = {action: ref for action, ref in mapping.items() if action in selected}
    runtime["discriminator"]["mapping"] = mapping
    runtime["oneOf"] = [item for item in runtime["oneOf"] if item["$ref"] in mapping.values()]
    definitions = runtime["$defs"]
    variants = {}
    for action, ref in mapping.items():
        variant = definitions[ref.rsplit("/", 1)[-1]]
        for name in exclude_fields:
            variant["properties"].pop(name, None)
            if name in variant.get("required", []):
                variant["required"].remove(name)
        variants[action] = variant

    by_field: dict[str, dict[str, dict]] = {}
    for action, variant in variants.items():
        for name, field in variant["properties"].items():
            by_field.setdefault(name, {})[action] = field
    properties = {}
    for name, fields in by_field.items():
        hints = list(dict.fromkeys(_shape_hint(field, definitions) for field in fields.values()))
        descriptions = list(
            dict.fromkeys(
                field["description"] for field in fields.values() if field.get("description")
            )
        )
        required = [action for action in fields if name in variants[action].get("required", [])]
        text = f"{' | '.join(hints)}. Actions: {', '.join(fields)}."
        if required:
            text += f" Required for: {', '.join(required)}."
        if descriptions:
            text += " " + " ".join(descriptions)
        if name == "result" and "create" in fields:
            text += " For create, a non-null result is required when state=done."
        if name in {"archive_note", "note"} and "archive" in fields:
            text += " For archive, provide archive_note or note."
        properties[name] = {
            "description": text,
            "x-runtime-schemas": copy.deepcopy(fields),
        }
    return {
        "type": "object",
        "description": (
            "Choose action and its domain fields. Create requires namespace and isolation_hint; "
            "create with state=done requires result. Archive requires namespace, task_id and "
            "archive_note or note. Update preserves state; use state or done to change it. "
            "All constraints are checked at runtime and reported as structured tool results."
        ),
        "properties": properties,
        "x-runtime-schema": runtime,
    }


def task_planning_schema(runtime_schema: dict) -> dict:
    """Legacy request wrapper, preserving its exact authoritative boundary schema."""
    request = task_action_planning_schema()
    request.pop("type")
    request.pop("x-runtime-schema")
    return {
        "type": "object",
        "properties": {"request": request},
        "x-runtime-schema": copy.deepcopy(runtime_schema),
    }
