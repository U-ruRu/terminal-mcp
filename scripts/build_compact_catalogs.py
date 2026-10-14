#!/usr/bin/env python3
"""Build/check exact future MCP tools/list contracts; no network, no deployment."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from terminal_mcp.mcp.compact_contracts import (  # noqa: E402
    ARTIFACTS,
    ROLE_NAMES,
    build,
    dump,
    encoded,
    generate,
)


def token_encoder():
    try:
        import tiktoken
    except ImportError as exc:
        raise SystemExit(
            "tiktoken==0.12.0 is required for deterministic metrics; "
            "install python -m pip install 'terminal-mcp[compact]'"
        ) from exc
    return tiktoken.get_encoding("cl100k_base")


def measure(obj, tokenizer, *, field: str | None = None) -> dict:
    value = obj[field] if field is not None else obj
    raw = encoded(value)
    text = raw.decode("utf-8")
    return {
        "utf8_bytes": len(raw),
        "characters": len(text),
        "tokens": len(tokenizer.encode(text, disallowed_special=())),
    }


def split_measure(catalog, enc):
    entire = measure(catalog, enc)
    out = {
        "serialized_tools_list": entire,
        "tools": {},
    }
    part_totals = {"descriptions": 0, "input_schemas": 0, "output_schemas": 0}
    for tool in catalog["tools"]:
        metric = measure(tool, enc)
        desc = measure(tool["description"], enc)
        inputs = measure(tool["inputSchema"], enc)
        outputs = measure(tool.get("outputSchema", {}), enc)
        metric["description"] = desc
        metric["input_schema"] = inputs
        metric["output_schema"] = outputs
        out["tools"][tool["name"]] = metric
        part_totals["descriptions"] += len(
            json.dumps(tool["description"], ensure_ascii=False).encode()
        )
        part_totals["input_schemas"] += inputs["utf8_bytes"]
        part_totals["output_schemas"] += outputs["utf8_bytes"]
    size = entire["utf8_bytes"]
    out["component_utf8_bytes"] = part_totals
    out["component_byte_shares"] = {
        name: round(count / size, 6) if size else 0 for name, count in part_totals.items()
    }
    return out


def delta(before, after):
    return {
        key: {
            "before": before[key],
            "after": after[key],
            "delta": after[key] - before[key],
            "change_pct": round(100 * (after[key] - before[key]) / before[key], 3)
            if before[key]
            else None,
        }
        for key in ("utf8_bytes", "characters", "tokens")
    }


def metrics(built, enc):
    roles = {}
    for role in ROLE_NAMES:
        original = split_measure(built["before"][role], enc)
        prospective = split_measure(built["after"][role], enc)
        rows = {
            name: delta(original["tools"][name], prospective["tools"][name])
            for name in original["tools"]
        }
        roles[role] = {
            "before": original,
            "after": prospective,
            "delta": delta(original["serialized_tools_list"], prospective["serialized_tools_list"]),
            "per_tool_delta": rows,
        }
    before_sum = {
        key: sum(r["before"]["serialized_tools_list"][key] for r in roles.values())
        for key in ("utf8_bytes", "characters", "tokens")
    }
    after_sum = {
        key: sum(r["after"]["serialized_tools_list"][key] for r in roles.values())
        for key in ("utf8_bytes", "characters", "tokens")
    }
    return {
        "tokenizer": "tiktoken==0.12.0 / cl100k_base",
        "serialization": "json.dumps(sort_keys=True,separators=(',', ':'),ensure_ascii=False)",
        "scope": (
            "the entire serialized tools/list result {tools:[...]}, "
            "separately for three endpoint roles"
        ),
        "roles": roles,
        "three_catalogs_total": delta(before_sum, after_sum),
    }


def outputs() -> dict[str, bytes]:
    generated = build()
    enc = token_encoder()
    report = metrics(generated, enc)
    expected = {
        "strict-contracts.json": dump(generated["strict"]),
        "coverage.json": dump(generated["coverage"]),
        "provenance.json": dump(generated["provenance"]),
        "metrics.json": dump(report),
    }
    for role in ROLE_NAMES:
        expected[f"before-{role}.json"] = dump(generated["before"][role])
        expected[f"catalog-{role}.json"] = dump(generated["after"][role])
    return expected


def check_artifacts(directory: Path, expected: dict[str, bytes]) -> list[str]:
    return [
        filename
        for filename, payload in expected.items()
        if not (directory / filename).is_file() or (directory / filename).read_bytes() != payload
    ]


def main() -> int:
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--write", action="store_true")
    g.add_argument("--check", action="store_true")
    args = p.parse_args()
    expected = outputs()
    # The runtime only needs small description/hash metadata; generated sidecars
    # remain offline review artifacts and are deliberately not loaded by FastMCP.
    import asyncio

    catalog = {}
    generated = build()
    native_before, native_after, _, native_coverage = asyncio.run(generate(mesh=False))
    for role in ROLE_NAMES:
        variants = [(role, generated["before"], generated["after"], generated["coverage"])]
        if role != "access":
            variants.append((role + "_native", native_before, native_after, native_coverage))
        for catalog_role, before, after, coverage in variants:
            catalog[catalog_role] = {}
            for item in after[role]["tools"]:
                key = role + "." + item["name"]
                catalog[catalog_role][item["name"]] = {
                    "description": item["description"],
                    "base_description": next(
                        previous["description"]
                        for previous in before[role]["tools"]
                        if previous["name"] == item["name"]
                    ),
                    "strict_input_sha256": coverage[key]["strict_input_sha256"],
                    "strict_output_sha256": coverage[key]["strict_output_sha256"],
                }
    catalog_path = REPO / "src/terminal_mcp/mcp/agent_catalogs.json"
    catalog_bytes = dump(catalog)
    if args.write:
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        for filename, payload in expected.items():
            (ARTIFACTS / filename).write_bytes(payload)
        catalog_path.write_bytes(catalog_bytes)
        print(f"Generated {len(expected)} files in {ARTIFACTS} and packaged agent_catalogs.json")
    else:
        stale = check_artifacts(ARTIFACTS, expected)
        if not catalog_path.is_file() or catalog_path.read_bytes() != catalog_bytes:
            stale.append("src/terminal_mcp/mcp/agent_catalogs.json")
        if stale:
            print("STALE / MISSING compact contracts: " + ", ".join(stale), file=sys.stderr)
            return 1
        print(f"PASS {len(expected)} deterministic compact contract artifacts")
    metric = json.loads(expected["metrics.json"])
    for role in ROLE_NAMES:
        total = metric["roles"][role]["delta"]
        print(
            f"{role}: bytes {total['utf8_bytes']['before']} -> {total['utf8_bytes']['after']} "
            f"({total['utf8_bytes']['delta']:+}); tokens "
            f"{total['tokens']['before']} -> {total['tokens']['after']}"
        )
    overall = metric["three_catalogs_total"]["utf8_bytes"]
    print(
        f"TOTAL: {overall['before']} -> {overall['after']} bytes "
        f"({overall['delta']:+}, {overall['change_pct']:+}%)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
