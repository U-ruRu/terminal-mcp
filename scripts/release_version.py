#!/usr/bin/env python3
"""Derive a monotonic prerelease version from installed and incoming artifacts.

Public MCP manifests and durable storage schema revisions bump the middle digit.
Other Python source changes bump the last digit. Build SHA is presentation-only.
The source checkout stays untouched; stamp only a disposable packaging copy.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path

SCHEMA_MANIFESTS = (
    "mcp/access_mesh_schema_baselines_v2.json",
    "mcp/role_schema_baselines_v1.json",
)
STORAGE_REVISIONS = (
    ("storage/sqlite.py", "SCHEMA_VERSION"),
    ("auth/foundation.py", "AUTH_SCHEMA_VERSION"),
)
# Optional for older installed releases that predate this independent Mesh
# migration marker. Missing v1 values count as a schema difference, not as
# a malformed package.
OPTIONAL_STORAGE_REVISIONS = (("storage/access_mesh_numbers.py", "MESH_NUMBERS_SCHEMA_VERSION"),)
VERSION_RX = re.compile(r'^__version__\s*=\s*["\'](\d+)\.(\d+)\.(\d+)["\']\s*$', re.MULTILINE)
SHA_RX = re.compile(r"[0-9a-fA-F]{7,40}\Z")


def semantic_version(package: Path) -> tuple[int, int, int]:
    source = (package / "version.py").read_text(encoding="utf-8")
    match = VERSION_RX.search(source)
    if match is None:
        raise ValueError("version.py must declare a three-part numeric __version__")
    return tuple(int(digit) for digit in match.groups())


def constant(path: Path, name: str) -> int | None:
    if not path.is_file():
        return None
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            if isinstance(node.value, ast.Constant) and type(node.value.value) is int:
                return node.value.value
    raise ValueError(f"cannot find static integer {name} in {path}")


def schema_fingerprint(package: Path) -> str:
    state: dict[str, object] = {}
    for name in SCHEMA_MANIFESTS:
        path = package / name
        # Missing manifests are significant; never silently treat absence as a match.
        state[name] = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    for name, symbol in STORAGE_REVISIONS:
        state[f"{name}:{symbol}"] = constant(package / name, symbol)
    for name, symbol in OPTIONAL_STORAGE_REVISIONS:
        try:
            state[f"{name}:{symbol}"] = constant(package / name, symbol)
        except ValueError:
            # A v1 Mesh implementation had no dedicated schema counter.
            state[f"{name}:{symbol}"] = None
    blob = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def code_fingerprint(package: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(package.rglob("*.py")):
        if path.name == "version.py" or "__pycache__" in path.parts:
            continue
        name = path.relative_to(package).as_posix().encode("utf-8")
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def calculate(
    package: Path, prior: Path | None, baseline: dict[str, object] | None = None
) -> dict[str, object]:
    candidate = semantic_version(package)
    next_schema_hash = schema_fingerprint(package)
    next_code_hash = code_fingerprint(package)
    if prior is not None and baseline is not None:
        raise ValueError("use either prior package or pinned prior baseline")
    if prior is not None:
        previous = semantic_version(prior)
        old_schema_hash = schema_fingerprint(prior)
        old_code_hash = code_fingerprint(prior)
    elif baseline is not None:
        previous = tuple(int(part) for part in str(baseline["version"]).split("."))
        if len(previous) != 3:
            raise ValueError("baseline version must contain three numeric digits")
        old_schema_hash = str(baseline["schema_fingerprint"])
        old_code_hash = str(baseline["code_fingerprint"])
        if any(len(value) != 64 for value in (old_schema_hash, old_code_hash)):
            raise ValueError("baseline fingerprints must be SHA256 hashes")
    else:
        previous = None
        old_schema_hash = None
        old_code_hash = None
    if previous is None:
        version = candidate
        kind = "initial"
    else:
        if previous[0] != 0 or candidate[0] != 0:
            raise ValueError("automatic prerelease bump requires major digit 0")
        if old_schema_hash != next_schema_hash:
            version = (0, previous[1] + 1, 0)
            kind = "schema"
        elif old_code_hash != next_code_hash:
            version = (0, previous[1], previous[2] + 1)
            kind = "code"
        else:
            version = previous
            kind = "unchanged"
        # A previously manually bumped source remains a valid lower bound.
        if candidate > version:
            version = candidate
    return {
        "version": ".".join(str(value) for value in version),
        "previous_version": ".".join(str(value) for value in previous) if previous else None,
        "bump": kind,
        "schema_fingerprint": next_schema_hash,
        "code_fingerprint": next_code_hash,
    }


def resolve_sha(explicit: str | None, source_repo: Path | None) -> str | None:
    if explicit:
        if not SHA_RX.fullmatch(explicit):
            raise ValueError("commit SHA must be 7-40 hexadecimal digits")
        return explicit.lower()[:7]
    if source_repo is None:
        return None
    result = subprocess.run(
        ["git", "-C", str(source_repo), "rev-parse", "--verify", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    sha = result.stdout.strip()
    return sha[:7].lower() if result.returncode == 0 and SHA_RX.fullmatch(sha) else None


def stamp(package: Path, result: dict[str, object], short_sha: str | None) -> None:
    package.mkdir(parents=True, exist_ok=True)
    version = str(result["version"])
    release_id = version + ("-" + short_sha if short_sha else "")
    (package / "version.py").write_text(
        f'__version__ = "{version}"\n'
        f'__build_sha__ = "{short_sha or ""}"\n'
        f'__release__ = "{release_id}"\n',
        encoding="utf-8",
    )
    result["commit_sha"] = short_sha
    result["release_id"] = release_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="project directory")
    parser.add_argument("--prior-package", type=Path, help="installed package directory")
    parser.add_argument("--prior-baseline", type=Path, help="last stable schema/code fingerprints")
    parser.add_argument("--sha", help="commit SHA provided by build pipeline")
    parser.add_argument("--source-repo", type=Path, help="git checkout for fallback commit SHA")
    parser.add_argument("--output", type=Path, help="write RELEASE_META.json")
    parser.add_argument(
        "--stamp", action="store_true", help="rewrite source/version.py in build copy"
    )
    args = parser.parse_args()
    package = args.source / "src/terminal_mcp"
    prior = args.prior_package
    if prior and not (prior / "version.py").is_file():
        parser.error("prior package is missing version.py")
    baseline = None
    if args.prior_baseline:
        baseline = json.loads(args.prior_baseline.read_text(encoding="utf-8"))
    result = calculate(package, prior, baseline=baseline)
    sha = resolve_sha(args.sha, args.source_repo)
    if args.stamp:
        if (args.source / ".git").exists():
            parser.error("--stamp refuses to modify a Git checkout; copy sources first")
        stamp(package, result, sha)
    else:
        result["commit_sha"] = sha
        result["release_id"] = str(result["version"]) + ("-" + sha if sha else "")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
