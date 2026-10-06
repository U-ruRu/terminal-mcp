#!/usr/bin/env python3
"""Attribute raw Fleet-control writes on a scrubbed /tmp or /var/tmp copy."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from terminal_mcp.fleet.writer_trace import trace_command


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sandbox-root", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    result = trace_command(
        args.sandbox_root,
        args.target,
        command,
        timeout_seconds=args.timeout,
    )
    print(json.dumps(result.public_dict(), sort_keys=True))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
