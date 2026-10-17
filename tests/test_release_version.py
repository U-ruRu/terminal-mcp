"""Automatic 0.schema.code release versions without mutating canonical sources."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/release_version.py"
spec = importlib.util.spec_from_file_location("release_version", SCRIPT)
assert spec and spec.loader
release_version = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_version)


def package(root: Path, version="0.14.4") -> Path:
    target = root / "src" / "terminal_mcp"
    (target / "mcp").mkdir(parents=True)
    (target / "storage").mkdir()
    (target / "auth").mkdir()
    (target / "version.py").write_text(f'__version__ = "{version}"\n')
    (target / "app.py").write_text("def run(): return 1\n")
    (target / "storage/sqlite.py").write_text("SCHEMA_VERSION = 22\n")
    (target / "auth/foundation.py").write_text("AUTH_SCHEMA_VERSION = 3\n")
    (target / "mcp/access_mesh_schema_baselines_v2.json").write_text(
        json.dumps({"roles": {"executor": {"tools": ["session"]}}}) + "\n"
    )
    (target / "mcp/role_schema_baselines_v1.json").write_text(
        json.dumps({"executor": ["session", "task_state"]}) + "\n"
    )
    return target


def test_code_only_change_increments_third_digit(tmp_path):
    before = package(tmp_path / "installed")
    after = package(tmp_path / "candidate")
    events_manifest(before, reason=True)
    events_manifest(after, reason=True)
    (after / "app.py").write_text("def run(): return 2\n")
    result = release_version.calculate(after, before)
    assert (result["version"], result["bump"]) == ("0.14.5", "code")


def test_public_schema_change_increments_middle_digit(tmp_path):
    before = package(tmp_path / "installed")
    after = package(tmp_path / "candidate")
    path = after / "mcp/access_mesh_schema_baselines_v2.json"
    path.write_text(json.dumps({"roles": {"executor": {"tools": ["session", "task_state"]}}}))
    (after / "app.py").write_text("def run(): return 2\n")
    result = release_version.calculate(after, before)
    assert (result["version"], result["bump"]) == ("0.15.0", "schema")


def test_stamp_writes_the_selected_public_release(tmp_path):
    candidate = package(tmp_path / "candidate")
    result = {"version": "0.14.5"}

    release_version.stamp(candidate, result, "abcdef0")

    assert result["release_id"] == "0.14.5-abcdef0"
    assert '__release__ = "0.14.5-abcdef0"' in (candidate / "version.py").read_text()


def events_manifest(target, *, reason=False, description="Original wording"):
    properties = {"agent_name": {"type": "string"}, "description": {"type": "string"}}
    required = ["agent_name"]
    if reason:
        properties["reason"] = {"type": "string", "minLength": 1}
        required.append("reason")
    value = {
        "events": [
            {
                "name": "session.ended",
                "description": description,
                "payloadSchema": {"type": "object", "properties": properties, "required": required},
            }
        ]
    }
    path = target / release_version.EVENT_SCHEMA_MANIFESTS[0]
    path.write_text(json.dumps(value))
    return path, value


def test_generated_mesh_events_contract_is_release_significant(tmp_path):
    previous = package(tmp_path / "installed", "0.23.0")
    incoming = package(tmp_path / "candidate")
    (incoming / release_version.EVENT_SCHEMA_MANIFESTS[1]).write_text(
        json.dumps({"maxWireBytes": 65536, "message-events/chunk": {"type": "object"}})
    )
    assert release_version.calculate(incoming, previous)["version"] == "0.24.0"
