#!/usr/bin/env python3
"""Generate/check pinned Mesh Events wire schemas; no network or runtime mutation."""

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from terminal_mcp.fleet.event_mesh import mesh_event_contracts  # noqa: E402

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--check", action="store_true")
args = parser.parse_args()
artifact = REPO / "docs/contracts/mcp-mesh-events.json"
package_artifact = REPO / "src/terminal_mcp/mcp/mesh_event_schema_baselines_v1.json"
generated = json.dumps(mesh_event_contracts(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
if args.check:
    if any(
        not path.exists() or path.read_text() != generated
        for path in (artifact, package_artifact)
    ):
        raise SystemExit("Mesh Events catalog is stale; run scripts/build_mesh_event_catalog.py")
    print("Mesh Events catalog is current.")
else:
    for path in (artifact, package_artifact):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(generated)
