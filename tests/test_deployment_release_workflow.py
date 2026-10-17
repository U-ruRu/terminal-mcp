"""Isolated journal fault tests plus real SQLite/WAL, HTTP and process boundaries."""

from __future__ import annotations

import io
import json
import sqlite3
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from terminal_mcp.deployment._release_common import ReleaseError, atomic, encoded, read_json, sha256
from terminal_mcp.deployment.artifact import (
    REQUIRED_GUARANTEES,
    approve,
    platform_id,
    unpack,
    verify_bundle,
)
from terminal_mcp.deployment.update import NodeDriver, Update, snapshot

SOURCE = "a" * 40
OLD = "b" * 40


def bundle_fixture(root: Path):
    bundle = root / "bundle"
    bundle.mkdir()
    content = b"accepted artifact\n"
    with tarfile.open(bundle / "payload.tar.gz", "w:gz") as tar:
        item = tarfile.TarInfo("payload.txt")
        item.size = len(content)
        item.mode = 0o644
        tar.addfile(item, io.BytesIO(content))
    (bundle / "dependencies.lock").write_text("example==1.0\n")
    manifest = {
        "format": 1,
        "source_sha": SOURCE,
        "source_tree": "c" * 40,
        "release": "0.26.0-aaaaaaa",
        "version": "0.26.0",
        "payload_sha256": sha256(bundle / "payload.tar.gz"),
        "dependencies_sha256": sha256(bundle / "dependencies.lock"),
        "unpacked_bytes": len(content),
        "schemas": {"runtime": 25, "auth": 3, "control": 3},
        "platform": platform_id(),
        "links": {},
        "sqlite": "3.53.4",
    }
    atomic(bundle / "manifest.json", encoded(manifest))
    report = {
        "schema": 1,
        "source_sha": SOURCE,
        "source_tree": "c" * 40,
        "mode": "full",
        "dirty": False,
        "source_changed": False,
        "exit_code": 0,
        "full_test_evidence_eligible": True,
        "deselected": 0,
        "verdict": "passed",
        "tests": {
            "collected": 8,
            "passed": 8,
            "failed": 0,
            "errors": 0,
            "skipped": 0,
            "xfail": 0,
            "xpass": 0,
        },
    }
    policy = {
        "source_sha": SOURCE,
        "approved_by": "isolated-test-only",
        "evidence": "test fixture, not a real acceptance",
        "accepted_exceptions": 0,
        "guarantees": sorted(REQUIRED_GUARANTEES),
        "upgrade_from": [OLD],
        "mesh": {"mode": "rolling", "evidence_sha256": "d" * 64, "compatible_from": [OLD]},
    }
    receipt, policy_file, approval = (
        root / "receipt.json",
        root / "policy.json",
        root / "approval.json",
    )
    report.update(
        state="completed",
        selection={"scope": "full"},
        python=platform_id()["python"],
        dependencies={"example": "1.0"},
        runtime={
            "sqlite_version": manifest["sqlite"],
            "python_version": manifest["platform"]["python"],
            "machine": manifest["platform"]["machine"],
            "implementation": manifest["platform"]["implementation"],
            "libc": manifest["platform"]["libc"],
        },
    )
    (root / "profile.json").write_text('{"deselected":[]}')
    (root / "junit.xml").write_text(
        '<testsuites><testsuite tests="8" failures="0" errors="0" skipped="0"/></testsuites>'
    )
    (root / "pytest.log").write_text("8 passed\n")
    report["files"] = {n: sha256(root / n) for n in ("profile.json", "junit.xml", "pytest.log")}
    atomic(receipt, encoded(report))
    atomic(policy_file, encoded(policy))
    result = approve(bundle, receipt, policy_file, approval)
    return bundle, approval, result["approval_sha256"]


class FakeNode:
    """Only OS service actuation is simulated; journal, archives and state live in tmp_path."""

    def __init__(self, root: Path, *, initial=False):
        self.root = root / "node"
        self.root.mkdir()
        self.profile_path = root / "node-profile.json"
        self.p = {"target": "isolated", "health_url": "http://127.0.0.1/health/live"}
        atomic(self.profile_path, encoded(self.p))
        self.services = ["terminal-mcp-executor.service", "terminal-mcp.service"]
        self.up = {u: not initial for u in self.services}
        self.calls = []
        self.error = None
        self.command_active = False
        self.old = self.root / "releases" / "old"
        if not initial:
            self.old.mkdir(parents=True)
            (self.root / "current").symlink_to(self.old)
        self.client = SimpleNamespace(health=self.health)
        self.migration_count = 0

    def current(self):
        link = self.root / "current"
        return link.resolve() if link.is_symlink() else None

    def health(self, url, expected_release=None, *, readiness=False):
        if not all(self.up.values()) or self.error == "health":
            raise ReleaseError("health_unavailable")
        return {"ok": True, "release": expected_release or "0.26.0-aaaaaaa"}

    def active(self, unit):
        return self.up[unit]

    def drained(self):
        if self.command_active:
            raise ReleaseError("active_commands_block_restart")

    def admission_guard(self, source_sha):
        assert source_sha == SOURCE

    def preflight(self, manifest, approval, *, initial=False):
        self.drained()
        if self.error in {"space", "schema", "mesh"}:
            raise ReleaseError("preflight_" + self.error)
        return {"current": str(self.current()) if self.current() else None, "initial": initial}

    def stage(self, bundle, destination, manifest):
        self.calls.append("stage")
        unpack(bundle, destination, manifest)
        if self.error == "stage":
            raise ReleaseError("stage_failed")

    def control(self, verb, unit):
        self.calls.append((verb, unit))
        if self.error == "start" and verb == "start":
            raise ReleaseError("service_start")
        self.up[unit] = verb == "start"
        if self.error == "start_unknown" and verb == "start":
            self.error = None
            raise ReleaseError("service_start_outcome_unknown")
        if self.error == "stop_unknown" and verb == "stop":
            self.error = None
            raise ReleaseError("service_stop_outcome_unknown")

    def check_peers(self, manifest, approval):
        return {}

    def ingress(self):
        if self.error == "ingress":
            raise ReleaseError("ingress_failed")
        return []

    def backup(self, release, destination):
        self.calls.append("backup")
        assert not any(self.up.values())
        destination.mkdir()
        atomic(destination / "snapshot.json", encoded({"stores": {}, "configs": {}}))
        if self.error == "backup":
            raise ReleaseError("backup_failed")

    def migrate(self, release, backup, receipt, source_sha):
        self.calls.append("migrate")
        self.migration_count += 1
        assert not any(self.up.values())
        if self.error == "migration_unknown":
            raise ReleaseError("migration_outcome_unknown")
        atomic(receipt, encoded({"completed": True, "source_sha": source_sha}))
        if self.error == "migration_receipted_unknown":
            self.error = None
            raise ReleaseError("migration_outcome_unknown")

    def verify_migration(self, backup, receipt, manifest):
        assert read_json(receipt)["completed"]

    def switch(self, release):
        self.calls.append("switch")
        return NodeDriver.switch(self, release)

    def ready(self, manifest):
        return self.health(self.p["health_url"], manifest["release"])


@pytest.fixture
def operation(tmp_path):
    bundle, approval, pin = bundle_fixture(tmp_path)
    node = FakeNode(tmp_path)
    return Update(node, bundle, approval, pin, SOURCE)


@pytest.mark.parametrize("custom_path", [False, True])
def test_update_env_keeps_durable_events_database_path(tmp_path, custom_path):
    """Execute the real updater's env-preservation function in a temp sandbox."""
    import subprocess

    installer = (Path(__file__).resolve().parents[1] / "deploy/install.sh").read_text()
    function = (
        "ensure_env_defaults(){"
        + installer.split("ensure_env_defaults(){", 1)[1].split("\n}\n", 1)[0]
        + "\n}\n"
    )
    env_file = tmp_path / "terminal-mcp.env"
    original = tmp_path / "custom.sqlite3" if custom_path else None
    env_file.write_text(
        'TERMINAL_MCP_DATABASE_PATH="' + str(original) + '"\n'
        if original
        else 'TERMINAL_MCP_HOST="127.0.0.1"\n'
    )
    command = (
        'set -e\nENV_FILE="$1"\nDATA="$2"\nCACHE="$3"\n'
        "ensure_console_origins(){ :; }\n" + function + "\nensure_env_defaults\n"
    )
    subprocess.run(
        [
            "bash",
            "-c",
            command,
            "--",
            str(env_file),
            str(tmp_path / "data"),
            str(tmp_path / "cache"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    location = str(original or (tmp_path / "data/terminal-mcp.sqlite3"))
    assert env_file.read_text().count('TERMINAL_MCP_DATABASE_PATH="' + location + '"') == 1


def test_apply_activates_the_approved_release(operation):
    operation.apply("isolated:idle-window")

    assert operation.driver.current() == operation.release


def test_active_command_blocks_before_stopping(operation):
    operation.driver.command_active = True
    with pytest.raises(ReleaseError, match="active_commands"):
        operation.apply("isolated:idle-window")
    assert operation.driver.calls == []


def test_bundle_verification_rejects_modified_payload(operation):
    with (operation.bundle / "payload.tar.gz").open("ab") as f:
        f.write(b"tampered")
    with pytest.raises(ReleaseError, match="artifact_digest"):
        verify_bundle(operation.bundle, SOURCE)


@pytest.mark.parametrize(
    "key,value",
    [
        ("mode", "dev"),
        ("dirty", True),
        ("source_changed", True),
        ("exit_code", 1),
        ("source_sha", OLD),
    ],
)
def test_approval_rejects_non_acceptance_receipt(tmp_path, key, value):
    bundle, _, _ = bundle_fixture(tmp_path)
    receipt = tmp_path / "receipt.json"
    data = read_json(receipt)
    data[key] = value
    atomic(receipt, encoded(data))
    with pytest.raises(ReleaseError):
        approve(bundle, receipt, tmp_path / "policy.json", tmp_path / "another.json")


@pytest.mark.parametrize(
    "name,kind,link",
    [
        ("../escape", "file", ""),
        ("/escape", "file", ""),
        ("safe/link", "link", "/etc/passwd"),
        ("fifo", "fifo", ""),
        ("hard", "hard", "payload.txt"),
    ],
)
def test_artifact_rejects_unsafe_members(tmp_path, name, kind, link):
    bundle, _, _ = bundle_fixture(tmp_path)
    with tarfile.open(bundle / "payload.tar.gz", "w:gz") as tar:
        member = tarfile.TarInfo(name)
        if kind == "link":
            member.type, member.linkname = tarfile.SYMTYPE, link
        elif kind == "fifo":
            member.type = tarfile.FIFOTYPE
        elif kind == "hard":
            member.type, member.linkname = tarfile.LNKTYPE, link
        else:
            member.size = 1
        tar.addfile(member, io.BytesIO(b"x") if kind == "file" else None)
    manifest = read_json(bundle / "manifest.json")
    with pytest.raises(ReleaseError):
        unpack(bundle, tmp_path / "out", manifest)
    assert not (tmp_path / "escape").exists()


def test_snapshot_copies_committed_wal_rows(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE current_state(value TEXT)")
        connection.execute("INSERT INTO current_state VALUES('durable')")
        connection.commit()

        result = snapshot({"stores": {"runtime": str(path)}}, tmp_path / "backup", [])

    copy = tmp_path / "backup" / result["stores"]["runtime"]["backup"]
    with sqlite3.connect(copy) as db:
        assert db.execute("SELECT value FROM current_state").fetchall() == [("durable",)]


@pytest.mark.parametrize("action", ["status", "resume", "recover", "_migrate"])
def test_unreadable_operation_state_does_not_claim_nothing_was_applied(
    monkeypatch,
    capsys,
    action,
):
    from terminal_mcp.deployment import release

    class BrokenDriver:
        def __init__(self, *args, **kwargs):
            raise ReleaseError("isolated_profile_unavailable")

    monkeypatch.setattr(release.os, "geteuid", lambda: 0)
    monkeypatch.setattr(release, "NodeDriver", BrokenDriver)
    monkeypatch.setattr(release, "offline_driver", lambda *args: BrokenDriver())
    arguments = SimpleNamespace(action=action, profile=Path("/isolated/missing"), target="isolated")
    monkeypatch.setattr(release, "parser", lambda: SimpleNamespace(parse_args=lambda: arguments))
    with pytest.raises(SystemExit) as exited:
        release.main()
    assert exited.value.code == 30
    output = json.loads(capsys.readouterr().out)
    assert output["outcome"] == "partially_applied"
    assert output["state_unknown"] is True
    assert output["error"] == "isolated_profile_unavailable"
