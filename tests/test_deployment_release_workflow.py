"""Isolated journal fault tests plus real SQLite/WAL, HTTP and process boundaries."""

from __future__ import annotations

import io
import json
import sqlite3
import sys
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from terminal_mcp.deployment._release_common import (
    ReleaseError,
    atomic,
    encoded,
    read_json,
    run,
    schema_versions,
    sha256,
)
from terminal_mcp.deployment.artifact import (
    REQUIRED_GUARANTEES,
    approve,
    platform_id,
    unpack,
    verify_approval,
    verify_bundle,
)
from terminal_mcp.deployment.probes import ProbeClient, checked_url
from terminal_mcp.deployment.update import NodeDriver, Update, preserved_rows, snapshot

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

    def health(self, url, expected_release=None):
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


def test_plan_status_are_read_only(operation):
    node = operation.driver
    before = sorted(str(p) for p in node.root.rglob("*"))
    assert operation.plan()["mutations"] == 0
    assert operation.status()["phase"] == "absent"
    assert operation.status()["phase"] == "absent"
    assert not node.calls
    assert before == sorted(str(p) for p in node.root.rglob("*"))


@pytest.mark.parametrize("initial", [False, True])
def test_install_update_and_idempotent_apply(tmp_path, initial):
    bundle, approval, pin = bundle_fixture(tmp_path)
    node = FakeNode(tmp_path, initial=initial)
    op = Update(node, bundle, approval, pin, SOURCE)
    assert op.apply("isolated:idle-window", initial=initial)["outcome"] == "applied"
    assert node.current() == op.release
    assert node.migration_count == 1
    assert node.calls.index("backup") < node.calls.index("migrate") < node.calls.index("switch")
    calls = node.calls[:]
    assert op.apply("isolated:idle-window")["outcome"] == "applied"
    assert op.status()["outcome"] == "applied"
    assert node.calls == calls


@pytest.mark.parametrize("error", ["space", "schema", "mesh"])
def test_preflight_blocks_without_service_mutation(operation, error):
    operation.driver.error = error
    with pytest.raises(ReleaseError, match="preflight"):
        operation.apply("isolated:idle-window")
    assert operation.driver.current() == operation.driver.old
    assert not operation.driver.calls
    assert operation.load() is None


def test_active_command_blocks_before_stopping(operation):
    operation.driver.command_active = True
    with pytest.raises(ReleaseError, match="active_commands"):
        operation.apply("isolated:idle-window")
    assert operation.driver.calls == []


def test_explicit_window_required(operation):
    with pytest.raises(ReleaseError, match="confirmation_required"):
        operation.apply("wrong")
    assert not operation.area.exists()


@pytest.mark.parametrize("error", ["stage", "backup", "stop_unknown"])
def test_safe_pre_migration_recovery_never_restores_database(operation, error):
    node = operation.driver
    node.error = error
    with pytest.raises(ReleaseError):
        operation.apply("isolated:idle-window")
    calls = node.calls[:]
    with pytest.raises(ReleaseError, match="not_blind_retry"):
        operation.apply("isolated:idle-window")
    assert calls == node.calls
    node.error = None
    assert operation.recover("isolated:idle-window")["phase"] == "aborted"
    assert all(node.up.values())
    assert node.current() == node.old
    assert node.migration_count == 0


def test_unknown_migration_blocks_replay_and_rollback(operation):
    node = operation.driver
    node.error = "migration_unknown"
    with pytest.raises(ReleaseError, match="outcome_unknown"):
        operation.apply("isolated:idle-window")
    assert operation.status()["phase"] == "migrating"
    assert not any(node.up.values())
    with pytest.raises(ReleaseError, match="offline_reconciliation"):
        operation.resume("isolated:idle-window")
    with pytest.raises(ReleaseError, match="rollback_forbidden"):
        operation.recover("isolated:idle-window")
    assert node.migration_count == 1


def test_completed_migration_timeout_reconciles_receipt_without_replay(operation):
    node = operation.driver
    node.error = "migration_receipted_unknown"
    with pytest.raises(ReleaseError, match="outcome_unknown"):
        operation.apply("isolated:idle-window")
    assert operation.resume("isolated:idle-window")["outcome"] == "applied"
    assert node.migration_count == 1


@pytest.mark.parametrize("error", ["start", "start_unknown", "health", "ingress"])
def test_post_migration_forward_only_recovery(operation, error):
    node = operation.driver
    node.error = error
    with pytest.raises(ReleaseError):
        operation.apply("isolated:idle-window")
    assert operation.status()["outcome"] == "partially_applied"
    assert operation.backup.exists()
    assert node.current() == operation.release
    with pytest.raises(ReleaseError, match="rollback_forbidden"):
        operation.recover("isolated:idle-window")
    before = node.calls.count(("start", node.services[0]))
    first_active = node.up[node.services[0]]
    node.error = None
    assert operation.resume("isolated:idle-window")["outcome"] == "applied"
    if first_active:
        assert node.calls.count(("start", node.services[0])) == before
    assert node.migration_count == 1


def test_profile_binding_drift_blocks_resume(operation):
    operation.driver.error = "stage"
    with pytest.raises(ReleaseError):
        operation.apply("isolated:idle-window")
    operation.binding["profile_sha256"] = "changed"
    with pytest.raises(ReleaseError, match="binding_changed"):
        operation.status()


def test_node_lock_rejects_concurrent_process_boundary(operation):
    with operation.lock():
        with pytest.raises(ReleaseError, match="already_running"):
            with operation.lock():
                pytest.fail("lock unexpectedly acquired")


def test_acceptance_pin_and_payload_tamper(operation):
    with pytest.raises(ReleaseError, match="approval_sha256_pin"):
        verify_approval(operation.bundle, operation.area / "absent", "", SOURCE)
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


def test_real_wal_backup_preserves_keys_and_expiry(tmp_path):
    dbfile = tmp_path / "runtime.sqlite3"
    connection = sqlite3.connect(dbfile)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE session_state(id TEXT, deadline TEXT, secret BLOB)")
    row = ("slot-1", "2026-10-13T08:00:00Z", b"private-test-material")
    connection.execute("INSERT INTO session_state VALUES(?,?,?)", row)
    connection.execute("PRAGMA user_version=24")
    connection.commit()
    key = tmp_path / "auth.sqlite3.access-key"
    key.write_bytes(b"test-key-kept-byte-identical")
    key.chmod(0o600)
    result = snapshot({"stores": {"runtime": str(dbfile)}}, tmp_path / "backup", [key])
    copy = tmp_path / "backup" / result["stores"]["runtime"]["backup"]
    with sqlite3.connect(copy) as db:
        assert db.execute("SELECT * FROM session_state").fetchone() == row
        assert db.execute("PRAGMA quick_check").fetchone() == ("ok",)
    preserved_rows(copy, dbfile)
    assert key.read_bytes() == b"test-key-kept-byte-identical"
    assert "private-test-material" not in json.dumps(result)
    assert (copy.stat().st_mode & 0o777) == 0o600
    connection.execute("UPDATE session_state SET deadline=?", ("renewed",))
    connection.commit()
    with pytest.raises(ReleaseError, match="rows_changed"):
        preserved_rows(copy, dbfile)
    connection.close()


def test_explicit_schema_markers_read_only(tmp_path):
    runtime, auth, control = [tmp_path / (s + ".sqlite3") for s in ("runtime", "auth", "control")]
    for path, version in ((runtime, 25), (auth, 3)):
        with sqlite3.connect(path) as db:
            db.execute(f"PRAGMA user_version={version}")
    with sqlite3.connect(control) as db:
        db.execute("CREATE TABLE control_schema(singleton,version)")
        db.execute("INSERT INTO control_schema VALUES(1,3)")
    before = [sha256(p) for p in (runtime, auth, control)]
    assert schema_versions(dict(runtime=str(runtime), auth=str(auth), control=str(control))) == {
        "runtime": 25,
        "auth": 3,
        "control": 3,
    }
    assert before == [sha256(p) for p in (runtime, auth, control)]


def test_real_subprocess_timeout_does_not_expose_secret():
    with pytest.raises(ReleaseError) as error:
        run(
            [sys.executable, "-c", "import time; print('secret-output'); time.sleep(5)"],
            timeout=0.05,
            code="test_process",
        )
    assert str(error.value) == "test_process_outcome_unknown"
    assert "secret-output" not in str(error.value)


@pytest.fixture
def http_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/cdn":
                self.send_response(403)
                self.send_header("cf-mitigated", "challenge")
                body = b"<html>challenge</html>"
            elif self.path == "/denied":
                self.send_response(403)
                body = b'{"error":"denied"}'
            elif self.path == "/html":
                self.send_response(200)
                body = b"<html>not the app</html>"
            else:
                self.send_response(200)
                body = b'{"ok":true,"version":"0.26.0","release":"0.26.0-aaaaaaa"}'
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join(2)


def test_real_http_client_distinguishes_application_cdn_and_html(http_server):
    client = ProbeClient()
    try:
        assert client.health(http_server + "/health", "0.26.0-aaaaaaa")["ok"]
        with pytest.raises(ReleaseError, match="live_release_mismatch"):
            client.health(http_server + "/health", "wrong")
        with pytest.raises(ReleaseError, match="blocked_by_cdn"):
            client.health(http_server + "/cdn")
        with pytest.raises(ReleaseError, match="health_http_failure"):
            client.health(http_server + "/denied")
        with pytest.raises(ReleaseError, match="not_application_json"):
            client.health(http_server + "/html")
    finally:
        client.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://remote.example",
        "https://user:password@example.com",
        "file:///etc/passwd",
        "https://x.test/?token=secret",
    ],
)
def test_probe_configuration_does_not_carry_secrets(url):
    with pytest.raises(ReleaseError):
        checked_url(url)


@pytest.mark.parametrize(
    "field,value",
    [
        ("sqlite_version", "3.46.1"),
        ("python_version", "0.0.0"),
        ("machine", "unknown-machine"),
        ("implementation", "unknown-implementation"),
        ("libc", ["unknown-libc", "0"]),
    ],
)
def test_acceptance_requires_the_same_native_runtime(tmp_path, field, value):
    from terminal_mcp.deployment.artifact import validate_receipt

    bundle, approval, _ = bundle_fixture(tmp_path)
    accepted = read_json(approval)
    report = accepted["receipt"]
    report["runtime"][field] = value
    with pytest.raises(ReleaseError, match="acceptance_native_runtime_mismatch"):
        validate_receipt(report, read_json(bundle / "manifest.json"), accepted["policy"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("full_test_evidence_eligible", False),
        ("deselected", 1),
        ("deselected", False),
    ],
)
def test_acceptance_refuses_ineligible_or_deselected_report(tmp_path, field, value):
    from terminal_mcp.deployment.artifact import validate_receipt

    bundle, approval, _ = bundle_fixture(tmp_path)
    accepted = read_json(approval)
    report = accepted["receipt"]
    report[field] = value
    with pytest.raises(ReleaseError):
        validate_receipt(report, read_json(bundle / "manifest.json"), accepted["policy"])


@pytest.mark.parametrize("deselected", [None, ["lost-test"], 1, False])
def test_real_profile_list_deselection_is_not_silently_accepted(tmp_path, deselected):
    from terminal_mcp.deployment.artifact import verify_evidence_files

    _, approval, _ = bundle_fixture(tmp_path)
    report = read_json(approval)["receipt"]
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({"deselected": deselected}))
    report["files"]["profile.json"] = sha256(path)
    with pytest.raises(ReleaseError, match="acceptance_deselected_tests"):
        verify_evidence_files(tmp_path / "receipt.json", report)
