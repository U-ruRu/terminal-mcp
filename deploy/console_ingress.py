#!/usr/bin/env python3
"""Idempotently ensure Terminal MCP Console/Fleet paths reach the local app."""

from __future__ import annotations

import argparse
from pathlib import Path
from urllib.parse import urlsplit

BEGIN = "# BEGIN terminal-mcp managed console ingress"
END = "# END terminal-mcp managed console ingress"
PATHS = (
    "/connect",
    "/pairing/exchange",
    "/actions/console",
    "/actions/console/*",
    "/console/*",
    "/internal/fleet/*",
)


def public_site(public_base_url: str) -> str:
    parsed = urlsplit(public_base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("public base URL must be absolute http(s)")
    return parsed.netloc


def managed_block(upstream: str, indent: str = "    ") -> str:
    paths = " ".join(PATHS)
    return "\n".join(
        (
            f"{indent}{BEGIN}",
            f"{indent}@terminal_mcp_console path {paths}",
            f"{indent}reverse_proxy @terminal_mcp_console {upstream}",
            f"{indent}{END}",
        )
    )


def _replace_managed(text: str, block: str) -> str | None:
    start = text.find(BEGIN)
    if start < 0:
        return None
    line_start = text.rfind("\n", 0, start) + 1
    finish = text.find(END, start)
    if finish < 0:
        raise ValueError("managed console ingress block has no END marker")
    line_end = text.find("\n", finish)
    if line_end < 0:
        line_end = len(text)
    else:
        line_end += 1
    return text[:line_start] + block + "\n" + text[line_end:]


def _site_header_matches(header: str, site: str) -> bool:
    raw = header.strip()
    if not raw or raw.startswith("#"):
        return False
    labels = [item.strip() for item in raw.split(",")]
    candidates = {site, f"https://{site}", f"http://{site}"}
    return any(label in candidates for label in labels)


def _find_site_block(text: str, site: str) -> tuple[int, int] | None:
    depth = 0
    block_start = None
    for offset, line in _lines_with_offsets(text):
        opens = line.count("{")
        closes = line.count("}")
        if depth == 0 and "{" in line:
            header = line.split("{", 1)[0]
            if _site_header_matches(header, site):
                block_start = offset + len(line)
        depth += opens - closes
        if block_start is not None and depth == 0:
            return block_start, offset
    return None


def _lines_with_offsets(text: str):
    offset = 0
    for line in text.splitlines(keepends=True):
        yield offset, line
        offset += len(line)


def _has_catch_all_proxy(block: str, upstream: str) -> bool:
    for line in block.splitlines():
        stripped = line.strip()
        if stripped == f"reverse_proxy {upstream}":
            return True
        if stripped.startswith(f"reverse_proxy {upstream} "):
            return True
    return False


def patch_caddyfile(text: str, *, site: str, upstream: str) -> str:
    replacement = _replace_managed(text, managed_block(upstream))
    if replacement is not None:
        return replacement

    bounds = _find_site_block(text, site)
    if bounds is None:
        suffix = "" if not text or text.endswith("\n") else "\n"
        return (
            text
            + suffix
            + f"{site} {{\n"
            + f"    {BEGIN}\n"
            + f"    reverse_proxy {upstream}\n"
            + f"    {END}\n"
            + "}\n"
        )

    body_start, body_end = bounds
    block = text[body_start:body_end]
    if _has_catch_all_proxy(block, upstream):
        return text

    return text[:body_start] + managed_block(upstream) + "\n" + text[body_start:]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--public-base-url", required=True)
    parser.add_argument("--upstream", default="127.0.0.1:8080")
    args = parser.parse_args()

    source = Path(args.input)
    target = Path(args.output)
    target.write_text(
        patch_caddyfile(
            source.read_text(),
            site=public_site(args.public_base_url),
            upstream=args.upstream,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
