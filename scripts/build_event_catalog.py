#!/usr/bin/env python3
"""Build/check the native Events catalog. No network or runtime changes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from terminal_mcp.mcp.event_store import event_catalog  # noqa: E402

ARTIFACT = REPO / "docs/contracts/mcp-events-list.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    generated = json.dumps(event_catalog(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.check:
        if not ARTIFACT.exists() or ARTIFACT.read_text() != generated:
            raise SystemExit("Native Events catalog is stale; run scripts/build_event_catalog.py")
        print("Native Events catalog is current.")
    else:
        ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
        ARTIFACT.write_text(generated)


if __name__ == "__main__":
    main()
