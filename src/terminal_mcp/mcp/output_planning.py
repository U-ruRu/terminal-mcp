"""Compact host-facing output typing, separate from strict wire validation."""

from collections import defaultdict


def flat_output_schema(schema: dict) -> dict:
    definitions = schema.get("$defs", {})

    def variants(node, seen=frozenset()):
        ref = node.get("$ref")
        if ref:
            if ref in seen or not ref.startswith("#/$defs/"):
                return [{}]
            key = ref.removeprefix("#/$defs/")
            return variants(definitions.get(key, {}), seen | {ref})
        for keyword in ("anyOf", "oneOf", "allOf"):
            if keyword in node:
                return [v for part in node[keyword] for v in variants(part, seen)]
        return [node]

    def simple(nodes):
        kinds = set()
        for node in nodes:
            for part in variants(node):
                kind = part.get("type")
                if kind is None:
                    return {}
                kinds.update(kind if isinstance(kind, list) else [kind])
        if not kinds:
            return {}
        result = {"type": next(iter(kinds)) if len(kinds) == 1 else sorted(kinds)}
        if "array" in kinds:
            result["items"] = {}
        return result

    fields = defaultdict(list)
    for branch in variants(schema):
        for name, value in branch.get("properties", {}).items():
            fields[name].append(value)
    properties = {name: simple(nodes) for name, nodes in sorted(fields.items())}
    return {
        "type": "object",
        "properties": properties,
        "required": ["ok"],
        "additionalProperties": False,
    }
