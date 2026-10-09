"""Automatic 0.schema.code release versions without mutating canonical sources."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

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
    (after / "app.py").write_text("def run(): return 2\n")
    result = release_version.calculate(after, before)
    assert (result["version"], result["bump"]) == ("0.14.5", "code")


def test_manifest_or_database_schema_change_increments_middle_digit(tmp_path):
    before = package(tmp_path / "installed")
    after = package(tmp_path / "candidate")
    path = after / "mcp/access_mesh_schema_baselines_v2.json"
    path.write_text(json.dumps({"roles": {"executor": {"tools": ["session", "task_state"]}}}))
    (after / "app.py").write_text("def run(): return 2\n")
    result = release_version.calculate(after, before)
    assert (result["version"], result["bump"]) == ("0.15.0", "schema")
    # Schema revision changes must also count when the public manifests stay identical.
    path.write_text((before / "mcp/access_mesh_schema_baselines_v2.json").read_text())
    (after / "storage/sqlite.py").write_text("SCHEMA_VERSION = 23\n")
    assert release_version.calculate(after, before)["version"] == "0.15.0"


def test_mesh_number_storage_migration_increments_schema_version(tmp_path):
    before = package(tmp_path / "installed")
    after = package(tmp_path / "candidate")
    path_before = before / "storage/access_mesh_numbers.py"
    path_after = after / "storage/access_mesh_numbers.py"
    # Old deployed Mesh files may exist without an explicit schema marker.
    path_before.write_text("class MeshSessionNumbers: pass\n")
    path_after.write_text("MESH_NUMBERS_SCHEMA_VERSION = 2\nclass MeshSessionNumbers: pass\n")
    result = release_version.calculate(after, before)
    assert (result["version"], result["bump"]) == ("0.15.0", "schema")
    # A subsequent code-only change on the same Mesh v2 schema is a patch.
    path_before.write_text(path_after.read_text())
    (after / "app.py").write_text("def run(): return 2\n")
    assert (
        release_version.calculate(after, before)["version"],
        release_version.calculate(after, before)["bump"],
    ) == ("0.14.5", "code")


def test_equivalent_json_and_non_runtime_file_do_not_bump(tmp_path):
    before = package(tmp_path / "installed")
    after = package(tmp_path / "candidate")
    path = after / "mcp/access_mesh_schema_baselines_v2.json"
    path.write_text('{ "roles": { "executor": { "tools" : [ "session" ] } } }\n')
    (after.parent.parent / "README.md").write_text("documentation changed")
    result = release_version.calculate(after, before)
    assert (result["version"], result["bump"]) == ("0.14.4", "unchanged")


def test_stamping_keeps_package_semver_and_short_commit_sha(tmp_path):
    before = package(tmp_path / "installed")
    after = package(tmp_path / "candidate")
    (after / "app.py").write_text("def run(): return 2\n")
    result = release_version.calculate(after, before)
    release_version.stamp(after, result, "abcdef0")
    assert release_version.semantic_version(after) == (0, 14, 5)
    assert result["release_id"] == "0.14.5-abcdef0"
    text = (after / "version.py").read_text()
    assert '__version__ = "0.14.5"' in text
    assert '__release__ = "0.14.5-abcdef0"' in text
    assert release_version.semantic_version(before) == (0, 14, 4)


def test_same_deployed_build_does_not_double_increment(tmp_path):
    before = package(tmp_path / "installed")
    after = package(tmp_path / "candidate")
    (after / "app.py").write_text("def run(): return 2\n")
    one = release_version.calculate(after, before)
    release_version.stamp(after, one, "abcdef0")
    after_redeploy = package(tmp_path / "redeploy", "0.14.4")
    (after_redeploy / "app.py").write_text("def run(): return 2\n")
    two = release_version.calculate(after_redeploy, after)
    assert (two["version"], two["bump"]) == ("0.14.5", "unchanged")


def test_invalid_commit_sha_rejected():
    for invalid in ("1234", "not-sha", "a" * 41, "12 34567"):
        with pytest.raises(ValueError):
            release_version.resolve_sha(invalid, None)
    assert release_version.resolve_sha("ABCDEF01234567890", None) == "abcdef0"


def test_manual_non_prerelease_is_never_auto_mutated(tmp_path):
    before = package(tmp_path / "installed", "1.0.0")
    after = package(tmp_path / "candidate", "1.0.0")
    (after / "app.py").write_text("def run(): return 2\n")
    with pytest.raises(ValueError, match="prerelease"):
        release_version.calculate(after, before)


def test_stamping_a_git_checkout_is_rejected(tmp_path):
    import subprocess
    import sys

    root = tmp_path / "checked-out"
    target = package(root)
    (root / ".git").write_text("gitdir: /private/irrelevant/worktree")
    original = (target / "version.py").read_text()
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--source",
            str(root),
            "--stamp",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert "refuses to modify a Git checkout" in proc.stderr
    assert (target / "version.py").read_text() == original


def test_current_http_health_includes_short_release_identifier(tmp_path):
    from fastapi.testclient import TestClient
    from test_access_mesh_mcp_runtime import settings

    from terminal_mcp.app import create_app
    from terminal_mcp.version import __release__, __version__

    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        body = client.get("/health/live").json()
    assert body["version"] == __version__
    assert body["release"] == __release__


def test_installer_packs_from_disposable_source_and_uses_canonical_baseline():
    script = (Path(__file__).resolve().parents[1] / "deploy/install.sh").read_text()
    assert 'BUILD_SOURCE="$STAGED_RELEASE/_build_source"' in script
    assert 'pip" install "$BUILD_SOURCE"' in script
    assert "CANONICAL_RELEASE.json" in script
    assert "QA_RELEASE.json" in script
    assert "TERMINAL_MCP_DEPLOY_CHANNEL" in script
    assert "scripts/release_version.py" in script


def test_legacy_qa_schema_fingerprint_remains_verifiable_after_marker_added(tmp_path):
    """Adding an optional schema marker must not rehash installed old packages."""
    import hashlib

    previous = package(tmp_path / "qa-without-mesh-revision")
    state = {}
    for name in release_version.SCHEMA_MANIFESTS:
        state[name] = json.loads((previous / name).read_text())
    for name, symbol in release_version.STORAGE_REVISIONS:
        state[f"{name}:{symbol}"] = release_version.constant(previous / name, symbol)
    source = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    original_hash = hashlib.sha256(source.encode()).hexdigest()
    assert release_version.schema_fingerprint(previous) == original_hash
    migrated = package(tmp_path / "new-mesh-schema")
    (migrated / "storage/access_mesh_numbers.py").write_text("MESH_NUMBERS_SCHEMA_VERSION = 2\n")
    assert release_version.schema_fingerprint(migrated) != original_hash


def test_consecutive_qa_builds_increment_code_on_same_schema_and_keep_sha(tmp_path):
    canonical = package(tmp_path / "canonical")
    first = package(tmp_path / "first-qa")
    manifest_file = first / "mcp/access_mesh_schema_baselines_v2.json"
    manifest_file.write_text(json.dumps({"roles": {"executor": {"tools": ["session", "state"]}}}))
    bump = release_version.calculate(first, canonical)
    assert (bump["version"], bump["bump"]) == ("0.15.0", "schema")
    release_version.stamp(first, bump, "abcdef0")
    release_version.verify_prior_metadata(first, bump)

    second = package(tmp_path / "second-qa")
    (second / "mcp/access_mesh_schema_baselines_v2.json").write_text(manifest_file.read_text())
    (second / "app.py").write_text("def run(): return 123\n")
    advanced = release_version.calculate(second, first)
    assert (advanced["version"], advanced["bump"]) == ("0.15.1", "code")
    release_version.stamp(second, advanced, "123abcd")
    release_version.verify_prior_metadata(second, advanced)

    rebuilt = package(tmp_path / "same-build")
    (rebuilt / "mcp/access_mesh_schema_baselines_v2.json").write_text(manifest_file.read_text())
    (rebuilt / "app.py").write_text((second / "app.py").read_text())
    repeat = release_version.calculate(rebuilt, second)
    assert (repeat["version"], repeat["bump"]) == ("0.15.1", "unchanged")
    assert release_version.resolve_sha("123abcdf1", None) == "123abcd"

    (rebuilt / "storage/sqlite.py").write_text("SCHEMA_VERSION = 23\n")
    another_schema = release_version.calculate(rebuilt, second)
    assert (another_schema["version"], another_schema["bump"]) == ("0.16.0", "schema")


def test_prior_qa_release_metadata_must_match_actual_installed_files(tmp_path):
    before = package(tmp_path / "prior")
    meta = {
        "version": "0.14.4",
        "schema_fingerprint": release_version.schema_fingerprint(before),
        "code_fingerprint": release_version.code_fingerprint(before),
        "commit_sha": "abcdef0",
        "release_id": "0.14.4-abcdef0",
    }
    release_version.verify_prior_metadata(before, meta)
    (before / "app.py").write_text("def run(): return 2\n")
    with pytest.raises(ValueError, match="code fingerprint"):
        release_version.verify_prior_metadata(before, meta)
