"""Selection, isolation, acceptance receipts and read-only probes are fail-closed."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import test_plan  # noqa: E402

SPEC = importlib.util.spec_from_file_location("tmcp_test_workflow", SCRIPTS / "test.py")
workflow = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(workflow)


def write(root, name, text=""):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.mark.parametrize(
    "changed",
    [
        "src/terminal_mcp/auth/foundation.py",
        "src/terminal_mcp/storage/sqlite.py",
        "src/terminal_mcp/fleet/protocol.py",
        "src/terminal_mcp/application/access_mesh.py",
        "src/terminal_mcp/core/public_errors.py",
        "scripts/build_compact_catalogs.py",
        "tests/conftest.py",
        "new-unknown-file.json",
        "tests/test_deleted.py",
    ],
)
def test_critical_unknown_or_deleted_changes_expand_to_full(tmp_path, changed):
    write(tmp_path, "tests/test_a.py", "def test_a(): pass\n")
    plan = test_plan.affected_plan(tmp_path, [changed])
    assert plan["scope"] == "full"
    assert plan["tests"] == ["tests"]
    assert plan["reasons"]


def test_test_helper_changes_select_transitive_consumers(tmp_path):
    write(tmp_path, "tests/test_helper.py", "def helper(): pass\n")
    write(tmp_path, "tests/test_consumer.py", "from test_helper import helper\n")
    write(tmp_path, "tests/test_indirect.py", "import test_consumer\n")
    write(tmp_path, "tests/test_unrelated.py", "def test_value(): pass\n")
    plan = test_plan.affected_plan(tmp_path, ["tests/test_helper.py"])
    assert plan["tests"] == [
        "tests/test_consumer.py",
        "tests/test_helper.py",
        "tests/test_indirect.py",
    ]
    assert len(plan["reasons"]) == 2


def test_unresolved_relative_helper_import_expands_to_full(tmp_path):
    write(tmp_path, "tests/test_a.py", "from .test_b import value\n")
    write(tmp_path, "tests/test_b.py", "value = 1\n")
    assert test_plan.affected_plan(tmp_path, ["tests/test_b.py"])["scope"] == "full"


def test_component_dependencies_include_real_integration_boundaries(tmp_path):
    for name in [
        "mesh_real_tcp",
        "oauth_retention",
        "sqlite_posix_lock_preservation",
        "public_contract",
        "events",
        "queue",
    ]:
        write(tmp_path, "tests/test_" + name + ".py")
    plan = test_plan.component_plan(tmp_path, ["mesh"])
    assert set(plan["components"]) == {"auth", "contracts", "events", "mesh", "storage"}
    assert "tests/test_mesh_real_tcp.py" in plan["tests"]
    assert "tests/test_sqlite_posix_lock_preservation.py" in plan["tests"]
    assert "tests/test_oauth_retention.py" in plan["tests"]
    assert "tests/test_queue.py" not in plan["tests"]


def test_isolated_runs_do_not_inherit_runtime_or_pytest_filters(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_MCP_AUTH_TOKEN", "do-not-inherit")
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k drop_all")
    monkeypatch.setenv("PYTHONOPTIMIZE", "2")
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    a = workflow.isolate(tmp_path, first, tmp_path)
    b = workflow.isolate(tmp_path, second, tmp_path)
    assert "TERMINAL_MCP_AUTH_TOKEN" not in a
    assert "PYTEST_ADDOPTS" not in a
    assert "PYTHONOPTIMIZE" not in a
    for name in workflow.STATE_PATHS:
        assert a["TERMINAL_MCP_" + name] != b["TERMINAL_MCP_" + name]
    assert a["PYTHONHASHSEED"] == "0"


@pytest.mark.parametrize(
    "argv",
    [
        ["full", "auth", "--plan"],
        ["full", "--changed-from", "HEAD", "--plan"],
        ["dev", "--expected-sha", "main", "--plan"],
        ["component", "invalid", "--plan"],
        ["node"],
        ["dev", "--timeout", "0", "--plan"],
    ],
)
def test_invalid_or_narrowed_acceptance_commands_are_blocked(argv):
    assert workflow.main(argv) == 2


def test_outcome_counts_preserve_fixture_errors_and_expected_failures():
    reports = [
        {"nodeid": "a", "phase": "call", "outcome": "passed"},
        {"nodeid": "b", "phase": "setup", "outcome": "failed"},
        {"nodeid": "c", "phase": "call", "outcome": "failed"},
        {"nodeid": "d", "phase": "call", "outcome": "skipped", "wasxfail": True},
        {"nodeid": "e", "phase": "call", "outcome": "passed", "wasxfail": True},
        {"nodeid": "f", "phase": "setup", "outcome": "skipped"},
        {"nodeid": "g", "phase": "call", "outcome": "passed"},
        {"nodeid": "g", "phase": "teardown", "outcome": "failed"},
    ]
    result = workflow.counts({"collected": 7, "reports": reports})
    assert result == {
        "collected": 7,
        "passed": 1,
        "failed": 1,
        "errors": 2,
        "skipped": 1,
        "xfail": 1,
        "xpass": 1,
    }


@pytest.mark.parametrize(
    "dirty,mode,exit_code,full_test_evidence_eligible",
    [
        ("", "full", 0, True),
        (" M test.py", "full", 0, False),
        ("", "dev", 0, False),
        ("", "full", 1, False),
    ],
)
def test_receipt_binds_exact_clean_full_success(
    tmp_path, monkeypatch, dirty, mode, exit_code, full_test_evidence_eligible
):
    sha = "1" * 40
    output = tmp_path / "report"
    monkeypatch.setattr(workflow, "git", lambda *a, **kw: dirty if a[0] == "status" else sha)
    monkeypatch.setattr(workflow.metadata, "distributions", lambda: [])

    def execute(command, *, env, stdout, **kwargs):
        profile = {
            "collected": 1,
            "reports": [
                {
                    "nodeid": "test_a",
                    "phase": "call",
                    "outcome": "passed" if exit_code == 0 else "failed",
                }
            ],
        }
        Path(env["TMCP_PROFILE_PATH"]).write_text(json.dumps(profile))
        stdout.write("result\n")
        (output / "junit.xml").write_text("<testsuites/>\n")
        return SimpleNamespace(returncode=exit_code)

    monkeypatch.setattr(workflow.subprocess, "run", execute)
    args = argparse.Namespace(
        expected_sha=sha,
        mode=mode,
        allow_dirty=True,
        output=str(output),
        timeout=30,
        sample=False,
        keep_state=False,
        fresh_preparation=False,
    )
    plan = {"scope": "full" if mode == "full" else "dev", "tests": ["tests"]}
    assert workflow.run(args, plan, root=tmp_path) == exit_code
    receipt = json.loads((output / "report.json").read_text())
    assert receipt["source_sha"] == sha
    assert receipt["full_test_evidence_eligible"] is full_test_evidence_eligible
    assert receipt["dirty"] is bool(dirty)
    assert len(receipt["files"]["junit.xml"]) == 64
    assert receipt["tests"]["collected"] == 1


def test_live_probe_is_read_only_and_compares_release():
    requests = []

    def serve(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "release": "0.25.1-26ae839"})

    result = workflow.node_check(
        "https://node.example", "0.25.1-26ae839", httpx.MockTransport(serve)
    )
    assert result["ok"] is True
    assert [(r.method, r.url.path) for r in requests] == [("GET", "/health/live")]
    assert (
        workflow.node_check("https://node.example", "different", httpx.MockTransport(serve))["ok"]
        is False
    )


def test_live_probe_reports_cdn_block_without_claiming_app_failure():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            403, headers={"cf-mitigated": "challenge"}, text="not application response"
        )
    )
    assert (
        workflow.node_check("https://node.example", "release", transport)["classification"]
        == "cdn_client_blocked"
    )


def test_live_probe_rejects_secrets_in_urls():
    with pytest.raises(ValueError, match="without credentials"):
        workflow.node_check("https://user:password@node.example", "release")
