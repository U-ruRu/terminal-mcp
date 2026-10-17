"""Offline structural compiler of Terminal MCP tool-local contracts.

Adapted algorithms (not domain types) from RKB structural compiler
e721d0bea8d47ef042d429f8651783cc5708efbe:
schema-context annotation stripping, conditional grammar, disjointness
proofs, per-tool $ref closure and exact fail-closed schema fallback.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from jsonschema import Draft202012Validator

from terminal_mcp.mcp.access import build_access_mcp
from terminal_mcp.mcp.access_contracts import (
    GetEventsInput,
    IssuerSessionInput,
)
from terminal_mcp.mcp.role_contracts import ROLE_TOOL_MODELS
from terminal_mcp.mcp.roles import build_role_mcp

ROOT = Path(__file__).resolve().parents[3]
ARTIFACTS = ROOT / "docs" / "compact-contracts" / "generated"
ANNOTATIONS = Path(__file__).with_name("compact_annotations.json")
SOURCE_FILES = (
    "src/terminal_mcp/mcp/role_contracts.py",
    "src/terminal_mcp/mcp/access_contracts.py",
    "src/terminal_mcp/mcp/task_contract.py",
    "src/terminal_mcp/mcp/task_planning.py",
    "src/terminal_mcp/mcp/output_contracts.py",
    "src/terminal_mcp/mcp/role_outputs.py",
    "src/terminal_mcp/mcp/output_planning.py",
    "src/terminal_mcp/mcp/roles.py",
    "src/terminal_mcp/mcp/access.py",
    "src/terminal_mcp/mcp/schema_manifest.py",
    "src/terminal_mcp/application/task_requests.py",
    "src/terminal_mcp/core/public_errors.py",
    "src/terminal_mcp/mcp/input_admission.py",
    "src/terminal_mcp/mcp/agent_contracts.py",
    "scripts/build_compact_catalogs.py",
    "pyproject.toml",
)
ROLE_NAMES = ("access", "executor", "coordinator")
ANNOTATION_KEYS = {"description", "title", "$comment", "examples", "example"}
MAP_KEYS = {"properties", "$defs", "definitions", "patternProperties", "dependentSchemas"}
ARRAY_SCHEMAS = {"allOf", "anyOf", "oneOf", "prefixItems"}
CHILD_SCHEMAS = {
    "items",
    "additionalProperties",
    "unevaluatedProperties",
    "propertyNames",
    "contains",
    "not",
    "if",
    "then",
    "else",
}
LITERAL_KEYS = {
    "const",
    "enum",
    "default",
    "required",
    "dependentRequired",
    "discriminator",
    "x-terminal-mcp-action-matrix",
}
OUTPUT_ROLES = ("success", "failure")


def encoded(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def dump(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    ).encode("utf-8")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def strip_annotations(value: Any, *, context: str = "schema") -> Any:
    """Only remove schema annotations, NEVER a user property named description/title."""
    if isinstance(value, list):
        return [strip_annotations(item, context=context) for item in value]
    if not isinstance(value, dict) or context == "literal":
        return copy.deepcopy(value)
    result = {}
    for key, item in value.items():
        if context == "schema" and key in ANNOTATION_KEYS:
            continue
        if context == "map":
            result[key] = strip_annotations(item, context="schema")
        elif key in MAP_KEYS:
            result[key] = strip_annotations(item, context="map")
        elif key in ARRAY_SCHEMAS or key in CHILD_SCHEMAS:
            result[key] = strip_annotations(item, context="schema")
        elif key in LITERAL_KEYS or key.startswith("x-"):
            result[key] = copy.deepcopy(item)
        else:
            # An unknown keyword is not discarded. Its exact value is retained.
            result[key] = copy.deepcopy(item)
    return result


def ref_name(ref: str) -> str:
    if not ref.startswith("#/$defs/") or "~" in ref:
        raise ValueError(f"Unsupported JSON Schema ref {ref!r}")
    return ref[len("#/$defs/") :]


def prove_disjoint(branches: list[dict], definitions: dict) -> bool:
    """Only prove disjointness for scalar types/values or REQUIRED discriminators."""

    def resolve(s, seen=None):
        seen = set() if seen is None else seen
        if not isinstance(s, dict):
            return s
        if "$ref" not in s:
            return s
        name = ref_name(s["$ref"])
        if name in seen:
            return None
        nxt = definitions.get(name)
        return resolve(nxt, seen | {name}) if nxt is not None else None

    def type_set(s):
        t = s.get("type")
        if t is None:
            return {"object"} if "properties" in s else set()
        return {("number" if v == "integer" else v) for v in (t if isinstance(t, list) else [t])}

    def disjoint(a, b):
        a, b = resolve(a), resolve(b)
        if not isinstance(a, dict) or not isinstance(b, dict):
            return False
        at, bt = type_set(a), type_set(b)
        if at and bt and at.isdisjoint(bt):
            return True
        aval = [a["const"]] if "const" in a else a.get("enum")
        bval = [b["const"]] if "const" in b else b.get("enum")
        if isinstance(aval, list) and isinstance(bval, list):
            if not any(type(x) is type(y) and x == y for x in aval for y in bval):
                return True
        common = set(a.get("required", [])) & set(b.get("required", []))
        if "object" in at and "object" in bt:
            return any(
                disjoint(a.get("properties", {}).get(k, {}), b.get("properties", {}).get(k, {}))
                for k in common
            )
        return False

    return bool(branches) and all(
        disjoint(a, b) for i, a in enumerate(branches) for b in branches[i + 1 :]
    )


def condition(schema: Any, render=None) -> str:
    """Logic-preserving shortening; uncommon shapes become exact SCHEMA JSON."""

    def raw(obj):
        return "SCHEMA " + encoded(obj).decode()

    if schema is True:
        return "TRUE"
    if schema is False:
        return "FALSE"
    if not isinstance(schema, dict):
        return raw(schema)
    if set(schema) == {"required"} and isinstance(schema["required"], list):
        return "requires(" + ",".join(schema["required"]) + ")"
    if set(schema) == {"not"}:
        node = schema["not"]
        if isinstance(node, dict) and set(node) == {"required"}:
            return "not_both(" + ",".join(node["required"]) + ")"
        return "NOT(" + condition(node, render) + ")"
    if {"properties", "required"} <= set(schema) and len(schema["properties"]) == 1:
        key = schema["required"][0] if len(schema["required"]) == 1 else ""
        prop = schema["properties"].get(key, {})
        if set(schema) == {"properties", "required"} and isinstance(prop, dict):
            if "const" in prop and len(prop) == 1:
                return key + "==" + encoded(prop["const"]).decode()
            if "enum" in prop and len(prop) == 1:
                return key + " in " + encoded(prop["enum"]).decode()
            if prop == {"not": {"type": "null"}}:
                return key + " present_non_null"
    if "if" in schema and set(schema) <= {"if", "then", "else"}:
        result = (
            "IF "
            + condition(schema["if"], render)
            + " => "
            + condition(schema.get("then", True), render)
        )
        if "else" in schema:
            result += "; ELSE => " + condition(schema["else"], render)
        return result
    if set(schema) == {"dependentRequired"}:
        return "; ".join(
            f"{k} requires({','.join(v)})" for k, v in schema["dependentRequired"].items()
        )
    if set(schema) == {"dependentSchemas"}:
        return "; ".join(
            f"IF present({k}) => {condition(v, render)}"
            for k, v in schema["dependentSchemas"].items()
        )
    for key, operator in (("allOf", "ALL_OF"), ("anyOf", "ANY_OF"), ("oneOf", "ONE_OF")):
        if set(schema) == {key}:
            parts = schema[key]
            if key == "anyOf" and all(
                isinstance(s, dict) and set(s) == {"required"} and len(s["required"]) == 1
                for s in parts
            ):
                return "require_at_least_one(" + ",".join(s["required"][0] for s in parts) + ")"
            if key == "allOf" and len(parts) >= 3:
                pairs = []
                for part in parts:
                    if (
                        not isinstance(part, dict)
                        or set(part) != {"not"}
                        or not isinstance(part["not"], dict)
                    ):
                        break
                    n = part["not"]
                    if set(n) != {"required"} or len(n["required"]) != 2:
                        break
                    pairs.append(tuple(sorted(n["required"])))
                if len(pairs) == len(parts):
                    fields = sorted({v for pair in pairs for v in pair})
                    if len(set(pairs)) == len(fields) * (len(fields) - 1) // 2:
                        return "mutually_exclusive(" + ",".join(fields) + ")"
            return operator + "(" + " | ".join(condition(p, render) for p in parts) + ")"
    if "properties" in schema and set(schema) <= {
        "properties",
        "required",
        "additionalProperties",
        "minProperties",
        "maxProperties",
    }:
        required = set(schema.get("required", []))
        parts = [
            f"{k}{'' if k in required else '?'}:{(render(v) if render else raw(v))}"
            for k, v in schema["properties"].items()
        ]
        if schema.get("additionalProperties") is False:
            parts.append("additional_fields: forbidden")
        for key in ("minProperties", "maxProperties"):
            if key in schema:
                parts.append(f"{key}={schema[key]}")
        return "{" + "; ".join(parts) + "}"
    return raw(schema)


def rules(schema: dict, render=None) -> list[str]:
    out = []
    for k in ("allOf", "anyOf", "oneOf", "if", "not", "dependentRequired", "dependentSchemas"):
        if k not in schema:
            continue
        if k == "if":
            out.append(
                condition({j: schema[j] for j in ("if", "then", "else") if j in schema}, render)
            )
        else:
            out.append(condition({k: schema[k]}, render))
    return out


class SchemaRenderer:
    """Per-tool reference closure. Unsupported assertions retain exact JSON fallback."""

    def __init__(self, root: dict, prefix: str):
        self.defs = root.get("$defs", {})
        self.prefix = prefix
        self.used: set[str] = set()
        self.fallbacks: list[str] = []

    def render(self, schema: Any, *, depth: int = 0, at: str = "$") -> str:
        if depth > 35:
            raise ValueError(f"Unbounded inline schema nesting at {at}")
        if isinstance(schema, bool):
            return "any" if schema else "never"
        if not isinstance(schema, dict):
            self.fallbacks.append(at)
            return "SCHEMA " + encoded(schema).decode()
        if "$ref" in schema:
            name = ref_name(schema["$ref"])
            if name not in self.defs:
                raise ValueError(f"Unresolved {self.prefix} schema reference: {name}")
            self.used.add(name)
            extra = {k: v for k, v in schema.items() if k != "$ref"}
            return name + ((" & " + self.render(extra, depth=depth + 1, at=at)) if extra else "")
        if "const" in schema and set(schema) <= {"const", "default"}:
            return encoded(schema["const"]).decode() + (
                " (default=" + encoded(schema["default"]).decode() + ")"
                if "default" in schema
                else ""
            )
        if "enum" in schema and set(schema) <= {"enum", "type", "default"}:
            return "|".join(encoded(v).decode() for v in schema["enum"]) + (
                " (default=" + encoded(schema["default"]).decode() + ")"
                if "default" in schema
                else ""
            )
        # JSON Schema default is merely an annotation: it can be moved
        # outside an otherwise standalone, exactly nullable union.
        if (
            set(schema) == {"anyOf", "default"}
            and len(schema["anyOf"]) == 2
            and schema["anyOf"].count({"type": "null"}) == 1
        ):
            other = next(branch for branch in schema["anyOf"] if branch != {"type": "null"})
            return (
                self.render(other, depth=depth + 1, at=at + "/anyOf")
                + "? (default="
                + encoded(schema["default"]).decode()
                + ")"
            )
        # Unrecognized union siblings are never silently dropped.
        if ("anyOf" in schema or "oneOf" in schema) and len(schema) != 1:
            self.fallbacks.append(at)
            return "SCHEMA_FALLBACK " + encoded(schema).decode()
        nullable = False
        types = schema.get("type", [])
        if isinstance(types, str):
            types = [types]
        types = list(types)
        if "null" in types and len(types) > 1:
            types.remove("null")
            nullable = True
        if ("anyOf" in schema or "oneOf" in schema) and len(schema) == 1:
            key = "oneOf" if "oneOf" in schema else "anyOf"
            branches = schema[key]
            nonnull = [v for v in branches if v != {"type": "null"}]
            if len(nonnull) == 1 and len(nonnull) + 1 == len(branches):
                return self.render(nonnull[0], depth=depth + 1, at=at + "/" + key) + "?"
            join = " | " if key == "anyOf" or prove_disjoint(branches, self.defs) else " ONE_OF "
            return (
                "("
                + join.join(
                    self.render(v, depth=depth + 1, at=f"{at}/{key}/{i}")
                    for i, v in enumerate(branches)
                )
                + ")"
            )
        if not types and "allOf" in schema and len(schema) == 1:
            return (
                "ALL_OF("
                + "; ".join(
                    self.render(v, depth=depth + 1, at=f"{at}/allOf/{i}")
                    for i, v in enumerate(schema["allOf"])
                )
                + ")"
            )
        parts = []
        core = types[0] if len(types) == 1 else None
        consumed = {
            "type",
            "$defs",
            "required",
            "allOf",
            "anyOf",
            "oneOf",
            "if",
            "then",
            "else",
            "not",
            "dependentRequired",
            "dependentSchemas",
        }
        if len(types) > 1:
            value = "(" + "|".join(types) + ")"
        elif core == "object" or "properties" in schema:
            consumed |= {"properties", "additionalProperties", "patternProperties", "propertyNames"}
            req = set(schema.get("required", []))
            fields = [
                f"{k}{'' if k in req else '?'}:"
                f"{self.render(v, depth=depth + 1, at=at + '/properties/' + k)}"
                for k, v in schema.get("properties", {}).items()
            ]
            for k, v in schema.get("patternProperties", {}).items():
                fields.append(
                    f"[{k}]:{self.render(v, depth=depth + 1, at=at + '/patternProperties/' + k)}"
                )
            if schema.get("additionalProperties") is False:
                fields.append("additional_fields: forbidden")
            elif isinstance(schema.get("additionalProperties"), dict):
                fields.append(
                    "[string]:"
                    + self.render(
                        schema["additionalProperties"],
                        depth=depth + 1,
                        at=at + "/additionalProperties",
                    )
                )
            elif schema.get("additionalProperties") is True:
                fields.append("[string]:any")
            value = "{ " + "; ".join(fields) + " }"
            if schema.get("propertyNames") is not None:
                parts.append(
                    "keys="
                    + self.render(
                        schema["propertyNames"], depth=depth + 1, at=at + "/propertyNames"
                    )
                )
        elif core == "array":
            consumed |= {"items", "prefixItems", "contains"}
            if "prefixItems" in schema:
                items = [
                    self.render(v, depth=depth + 1, at=at + f"/prefixItems/{i}")
                    for i, v in enumerate(schema["prefixItems"])
                ]
                if schema.get("items") is not False:
                    items.append(
                        "..."
                        + self.render(schema.get("items", {}), depth=depth + 1, at=at + "/items")
                    )
                value = "[" + ",".join(items) + "]"
            else:
                value = (
                    self.render(schema.get("items", {}), depth=depth + 1, at=at + "/items") + "[]"
                )
            if "contains" in schema:
                parts.append(
                    "contains="
                    + self.render(schema["contains"], depth=depth + 1, at=at + "/contains")
                )
        elif core in ("integer", "number"):
            value = "int" if core == "integer" else "number"
        elif core == "string":
            value = "string"
            if "minLength" in schema or "maxLength" in schema:
                value += f"({schema.get('minLength', 0)}..{schema.get('maxLength', '∞')})"
                consumed |= {"minLength", "maxLength"}
        else:
            value = core or "any"
        if nullable:
            value += "?"
        for k, label in (
            ("minimum", ">="),
            ("maximum", "<="),
            ("exclusiveMinimum", ">"),
            ("exclusiveMaximum", "<"),
            ("multipleOf", "multipleOf="),
            ("minItems", "items>="),
            ("maxItems", "items<="),
            ("minProperties", "fields>="),
            ("maxProperties", "fields<="),
            ("minContains", "contains>="),
            ("maxContains", "contains<="),
        ):
            if k in schema:
                consumed.add(k)
                parts.append(f"{label}{schema[k]}")
        for k in ("pattern", "format", "uniqueItems", "default", "minLength", "maxLength"):
            if k in schema:
                consumed.add(k)
                if k in ("minLength", "maxLength") and core == "string":
                    continue
                parts.append(f"{k}={encoded(schema[k]).decode()}")
        r = rules(schema, lambda s: self.render(s, depth=depth + 1, at=at + "/condition"))
        if r:
            parts.extend("rule: " + v for v in r)
        # Unhandled keyword values are included verbatim in the descriptor.
        unknown = {k: schema[k] for k in sorted(set(schema) - consumed)}
        if unknown:
            self.fallbacks.append(at)
            parts.append("SCHEMA_FALLBACK " + encoded(unknown).decode())
        return value + (" (" + "; ".join(parts) + ")" if parts else "")

    def definitions(self) -> list[str]:
        printed = set()
        result = []
        while True:
            pending = sorted(self.used - printed)
            if not pending:
                break
            for name in pending:
                printed.add(name)
                shape = self.defs[name]
                result.append(f"{name}={self.render(shape, at='#/$defs/' + name)}")
                if len(result) > 120:
                    raise ValueError("Unexpected tool-local type closure >120")
        return result


def output_summary(schema: dict) -> str:
    """Summarize top-level alternatives; full outputSchema is the exact fallback."""
    defs = schema.get("$defs", {})
    terms = schema.get("anyOf", schema.get("oneOf", [schema]))
    entries = []
    for branch in terms:
        if "$ref" in branch:
            name = ref_name(branch["$ref"])
            if name not in defs:
                raise ValueError(f"Output $ref {name} not found")
            obj = defs[name]
        else:
            name, obj = "inline", branch
        if "oneOf" in obj and not obj.get("properties"):
            options = [ref_name(v["$ref"]) if "$ref" in v else "inline" for v in obj["oneOf"]]
            entries.append(f"{name}:ONE_OF({','.join(options)})")
            continue
        req = set(obj.get("required", []))
        ps = obj.get("properties", {})
        if not ps:
            entries.append(name + " (exact outputSchema fallback)")
            continue
        fields = []
        for key, field in ps.items():
            if "$ref" in field:
                typ = ref_name(field["$ref"])
            elif "const" in field:
                typ = encoded(field["const"]).decode()
            elif "type" in field:
                t = field["type"]
                typ = "|".join(t) if isinstance(t, list) else t
            elif "anyOf" in field or "oneOf" in field:
                typ = "union"
            else:
                typ = "schema"
            fields.append(f"{key}{'' if key in req else '?'}:{typ}")
        entries.append(name + "{" + ",".join(fields) + "}")
    return " | ".join(entries)


def contract_models(role: str) -> dict[str, type]:
    if role == "access":
        return {"session": IssuerSessionInput, "get_events": GetEventsInput}
    return {name: model for (r, name), model in ROLE_TOOL_MODELS.items() if r == role}


def servers() -> dict:
    service = SimpleNamespace(persistent=None, access_mesh=object())
    return {
        "access": build_access_mcp(SimpleNamespace(service=service), publish_agent_contract=False),
        "executor": build_role_mcp(service, "executor", publish_agent_contract=False),
        "coordinator": build_role_mcp(service, "coordinator", publish_agent_contract=False),
    }


def _annotations() -> dict:
    parsed = json.loads(ANNOTATIONS.read_text())
    for roletool, values in parsed.items():
        if not isinstance(values, list):
            raise ValueError(f"Invalid annotation group {roletool}")
        for entry in values:
            path = ROOT / entry["source"]
            if (
                not path.is_file()
                or not entry.get("anchor")
                or entry["anchor"] not in path.read_text()
            ):
                raise ValueError(f"Annotation source stale: {roletool}: {entry}")
            if not entry.get("text"):
                raise ValueError(f"Empty semantic annotation: {roletool}")
            # Runtime rules may be action-scoped. Validate the scope against
            # the real canonical action registry so documentation cannot
            # silently drift to a nonexistent or renamed action.
            if "action" in entry:
                role, tool_name = roletool.split(".", 1)
                if role == "*" or entry["action"] not in action_models(role, tool_name):
                    raise ValueError(f"Invalid action-scoped annotation: {roletool}: {entry}")
    return parsed


def source_digests() -> dict[str, str]:
    names = list(SOURCE_FILES) + [
        "src/terminal_mcp/mcp/compact_contracts.py",
        "src/terminal_mcp/mcp/compact_annotations.json",
    ]
    return {f: sha((ROOT / f).read_bytes()) for f in names}


def action_models(role: str, name: str) -> dict[str, type]:
    """Canonical post-boundary validators, not the permissive role union."""
    if (role, name) == ("coordinator", "task_manage"):
        from terminal_mcp.mcp.roles import _TASK_MANAGE_MODELS

        return dict(_TASK_MANAGE_MODELS)
    if (role, name) == ("executor", "task_claim"):
        from terminal_mcp.application.task_requests import TaskClaimRequest, TaskReleaseRequest

        return {"claim": TaskClaimRequest, "release": TaskReleaseRequest}
    if (role, name) == ("executor", "task_comment"):
        from terminal_mcp.application.task_requests import TaskCheckpointRequest, TaskCommentRequest

        return {"comment": TaskCommentRequest, "checkpoint": TaskCheckpointRequest}
    return {}


def action_contracts(role: str, name: str, properties: dict) -> tuple[list[str], dict]:
    """Generate full per-action whitelists/required fields from real server models."""
    selected = action_models(role, name)
    if not selected:
        return [], {}
    rows = []
    closure = {}
    available = set(properties)
    for action, model in selected.items():
        # Only fields actually accepted by the public surface can be offered.
        allowed = set(model.model_fields) & available
        required = {f for f in allowed if model.model_fields[f].is_required()}
        # The outer public boundary can supply a default discriminator before
        # canonical conversion (e.g. MeshTaskCommentInput defaults to comment).
        # It is not a required field in the original client request.
        surface_action = contract_models(role)[name].model_fields.get("action")
        if (
            surface_action is not None
            and not surface_action.is_required()
            and surface_action.default == action
        ):
            required.discard("action")
        # Require specific non-null values only where model annotation disallows None.
        # This distinction is crucial for nullable fields in the outer role boundary.
        model_schema = model.model_json_schema()
        if model_schema.get("additionalProperties") is not False:
            raise ValueError(f"Canonical {model.__name__} became open")
        if not required.issubset(allowed):
            raise ValueError(f"Unreachable canonical requirement in {model.__name__}")
        extras = sorted(available - allowed)
        payload = {
            "source": model.__module__ + "." + model.__qualname__,
            "source_schema_sha256": sha(encoded(model_schema)),
            "allowed": sorted(allowed),
            "required_non_null": sorted(required),
            "forbidden": extras,
            "closed": True,
        }
        closure[action] = payload
        rows.append(
            f"{action}: allowed fields [{', '.join(payload['allowed'])}]; "
            f"required non-null [{', '.join(payload['required_non_null']) or '(none)'}]; "
            "all other named inputs rejected as extra_forbidden"
        )
    # Ensure no newly introduced server action can silently disappear from discovery.
    if (role, name) == ("coordinator", "task_manage"):
        from terminal_mcp.application.base import ROLE_TASK_ACTIONS

        expected = set(ROLE_TASK_ACTIONS["coordinator"])
        if expected != set(selected):
            raise ValueError(f"Coordinator action roster drift {expected ^ set(selected)}")
    return rows, closure


def transport_input_schema(strict_schema: dict, effective_before: dict) -> dict:
    """Only MCP's object envelope is public. Runtime validators remain authoritative."""
    return {"type": "object"}


def compile_tool(
    role: str, item: dict, model: type, notes: list[dict], strict_output: dict
) -> tuple[dict, dict, dict]:
    strict_input = model.model_json_schema()
    # The actual strict output model is already attached to FastMCP tool metadata.
    # Provided by caller as _strict_output only for compilation, not published.
    Draft202012Validator.check_schema(strict_input)
    Draft202012Validator.check_schema(strict_output)
    assert strict_input.get("type") == "object"
    # Structural RKB rule: discard schema-annotation metadata in schema context
    # before rendering. Never strip literal user keys named title/description.
    descriptive_input = strip_annotations(strict_input)
    renderer = SchemaRenderer(descriptive_input, f"{role}.{item['name']}.input")
    props = descriptive_input.get("properties", {})
    required = set(descriptive_input.get("required", []))
    fields = [
        f"{k}{'' if k in required else '?'}:{renderer.render(v, at='$/properties/' + k)}"
        for k, v in props.items()
    ]
    conds = rules(descriptive_input, renderer.render)
    published = copy.deepcopy(item)
    # All future client-side validation apart from the protocol-level
    # object envelope is advisory. Strict input stays in the source sidecar
    # and remains exactly the existing FastMCP runtime boundary.
    published["inputSchema"] = transport_input_schema(strict_input, item["inputSchema"])
    published["outputSchema"] = {"type": "object"}
    # Precise runtime output models remain the authoritative checked sidecar.
    action_lines, action_coverage = action_contracts(role, item["name"], props)
    local_defs = renderer.definitions()
    lines = [
        item.get("description") or "",
        "Arguments (field? optional; Type? nullable): "
        + ("; ".join(fields) if fields else "(none)")
        + ".",
    ]
    if local_defs:
        lines.append("Types used by " + item["name"] + " only: " + "; ".join(local_defs) + ".")
    if action_lines:
        lines.append("rules (per action): " + "; ".join(action_lines) + ".")
    if conds:
        lines.append("rules (schema): " + "; ".join(conds) + ".")
    if notes:
        # Distinguish per-action post-schema runtime gates from general rules;
        # keep each source anchor in the generated coverage provenance.
        rendered_rules = [
            (f"action={note['action']}: " if note.get("action") else "") + note["text"]
            for note in notes
        ]
        lines.append("rules (server): " + "; ".join(rendered_rules) + ".")
    lines.append("Returns: " + output_summary(strict_output) + ".")
    lines.append(
        "Errors: input_validation_failed (types, fields, values, conditions); "
        "input_admission_failed (oversize/complex input); unchanged authorization and "
        "domain errors in error.code. Validation failures return structuredContent "
        "{ok:false,error:{code,reason,path,expected?},outcome:not_committed}."
    )
    published["description"] = "\n".join(lines)
    strict = {"inputSchema": strict_input, "outputSchema": strict_output}
    coverage = {
        "model": model.__module__ + "." + model.__qualname__,
        "strict_input_sha256": sha(encoded(strict_input)),
        "strict_output_sha256": sha(encoded(strict_output)),
        "published_input_sha256": sha(encoded(published["inputSchema"])),
        "published_output_sha256": sha(encoded(published["outputSchema"])),
        "input_fields": list(props),
        "required_fields": sorted(required),
        "conditions": conds,
        "local_types": sorted(renderer.used),
        "exact_input_fallbacks": sorted(set(renderer.fallbacks)),
        "output_fallback": "strict-contracts.json:" + role + "." + item["name"] + ".outputSchema",
        "runtime_annotations": [
            {
                "source": note["source"],
                "anchor": note["anchor"],
                "text": note["text"],
                **({"action": note["action"]} if note.get("action") else {}),
            }
            for note in notes
        ],
        "baseline_description_preserved": True,
        "public_validation_boundary": "object-only / all named & unknown argument values forwarded",
        "action_models": action_coverage,
    }
    Draft202012Validator.check_schema(published["inputSchema"])
    Draft202012Validator.check_schema(published["outputSchema"])
    return published, strict, coverage


async def generate() -> tuple[dict, dict, dict, dict]:
    sources, after, coverage, before = {}, {}, {}, {}
    notes = _annotations()
    built = servers()
    for role in ROLE_NAMES:
        server = built[role]
        names = contract_models(role)
        base_tools = [
            t.model_dump(mode="json", by_alias=True, exclude_none=True)
            for t in await server.list_tools()
        ]
        expected = sorted(names)
        actual = sorted(t["name"] for t in base_tools)
        if expected != actual:
            raise ValueError(f"{role} strict/published roster mismatch: {expected} != {actual}")
        strict_by_name = {
            t.name: t.fn_metadata.output_model.model_json_schema()
            for t in server._tool_manager.list_tools()
        }
        before[role] = {"tools": base_tools}
        current = []
        for base in base_tools:
            name = base["name"]
            key = role + "." + name
            selected = notes.get(key, []) + notes.get("*." + name, [])
            new, source, rule_coverage = compile_tool(
                role, base, names[name], selected, strict_by_name[name]
            )
            current.append(new)
            sources[key] = source
            coverage[key] = rule_coverage
        after[role] = {"tools": current}
    return before, after, sources, coverage


def build() -> dict[str, Any]:
    before, after, source, coverage = asyncio.run(generate())
    provenance = {
        "compiler": "Terminal structural Python compiler (RKB V4 algorithms)",
        "rkb_sha": "e721d0bea8d47ef042d429f8651783cc5708efbe",
        "source_sha256": source_digests(),
        "roles": {role: [t["name"] for t in after[role]["tools"]] for role in ROLE_NAMES},
    }
    return {
        "before": before,
        "after": after,
        "strict": source,
        "coverage": coverage,
        "provenance": provenance,
    }
