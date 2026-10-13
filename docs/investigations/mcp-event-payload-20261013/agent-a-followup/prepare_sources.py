"""Fetch only reviewed probe inputs; install hash-locked JS dependencies without scripts.

No third-party server/bootstrap code is executed. Existing sources must match the
manifest. Pass --output to a private scratch directory; no runtime files are used.
"""

import argparse
import hashlib
import json
import subprocess
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument("--output", required=True)
args = parser.parse_args()
root = Path(args.output).resolve()
root.mkdir(parents=True, exist_ok=True)
manifest = json.loads((HERE / "sources.json").read_text())
wanted = {
    "hookdeck/mcp-events-bridge/src/core/sign.ts",
    "rokrokss/recly/experiments/dot-probe/probe.mjs",
    "openai-mcp-events.md",
    "upstash/agentkit/packages/mcp-toolkit/src/events/webhooks.ts",
}
for source in manifest["sources"]:
    entries = (
        [(source["repo"] + "/" + f["path"], f) for f in source["files"]]
        if "repo" in source
        else [(source.get("cache_path", ""), source)]
    )
    for relative, entry in entries:
        if relative not in wanted:
            continue
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raw = target.read_bytes()
        else:
            with urllib.request.urlopen(
                urllib.request.Request(
                    entry["url"], headers={"User-Agent": "Terminal-MCP-research"}
                ),
                timeout=20,
            ) as response:
                raw = response.read()
        if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise ValueError("reviewed_source_hash_mismatch: " + relative)
        target.write_bytes(raw)
js = root / "js"
js.mkdir(exist_ok=True)
(js / "package.json").write_bytes((HERE / "node-package.json").read_bytes())
(js / "package-lock.json").write_bytes((HERE / "node-package-lock.json").read_bytes())
subprocess.run(
    ["npm", "ci", "--prefix", str(js), "--ignore-scripts", "--no-audit", "--no-fund"],
    check=True,
    timeout=60,
)
print("Prepared reviewed source cache:", root)
