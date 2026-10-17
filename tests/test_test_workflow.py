"""Selection, isolation, acceptance receipts and read-only probes are fail-closed."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

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


def test_test_helper_changes_select_transitive_consumers(tmp_path, monkeypatch):
    monkeypatch.setattr(
        test_plan,
        "COMPONENT_MODULES",
        frozenset(
            {
                "test_helper.py",
                "test_consumer.py",
                "test_indirect.py",
                "test_unrelated.py",
            }
        ),
    )
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


def test_mixed_layers_preserve_component_class_tests(tmp_path, monkeypatch):
    write(
        tmp_path,
        "tests/test_compact_contracts.py",
        (
            "def test_value(): pass\n"
            "def test_app(): pass\n"
            "class TestCurrent:\n    def test_method(self): pass\n"
        ),
    )
    monkeypatch.setattr(
        test_plan,
        "MIXED_TEST_LAYERS",
        {
            "test_compact_contracts.py": {"integration": ("test_app",)},
        },
    )
    plan = test_plan.component_plan(tmp_path, ["contracts"])
    assert plan["tests"] == [
        "tests/test_compact_contracts.py::TestCurrent::test_method",
        "tests/test_compact_contracts.py::test_value",
    ]
    assert plan["deferred"]["integration"] == ["tests/test_compact_contracts.py::test_app"]


def test_run_refuses_empty_selection_before_reading_source_identity(tmp_path):
    with pytest.raises(ValueError, match="Empty test selection"):
        workflow.run(argparse.Namespace(), {"tests": []}, root=tmp_path)


def test_runner_isolates_inherited_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_MCP_AUTH_TOKEN", "do-not-inherit")
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k drop_all")
    monkeypatch.setenv("PYTHONOPTIMIZE", "2")
    state = tmp_path / "state"
    state.mkdir()

    env = workflow.isolate(tmp_path, state, tmp_path)

    assert {"TERMINAL_MCP_AUTH_TOKEN", "PYTEST_ADDOPTS", "PYTHONOPTIMIZE"}.isdisjoint(env)
    assert env["TMPDIR"] == str(state)


def test_full_rejects_changed_file_selection():
    assert workflow.main(["full", "--changed-from", "HEAD", "--plan"]) == 2


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
    monkeypatch.setattr(workflow.os, "getloadavg", lambda: (0.0, 0.0, 0.0), raising=False)
    monkeypatch.setattr(workflow.platform, "machine", lambda: "test-machine")
    monkeypatch.setattr(workflow.platform, "libc_ver", lambda: ("test-libc", "1"))
    monkeypatch.setattr(workflow.platform, "python_version", lambda: "3.12.12")

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
    assert receipt["full_test_evidence_eligible"] is full_test_evidence_eligible


@pytest.mark.parametrize(
    "outcome,keep_state,expected",
    [
        ("failed", False, 1),
        ("timeout", False, 124),
        ("error", False, None),
        ("failed", True, 1),
    ],
)
def test_runner_cleanup_on_failures_and_keep_state(
    tmp_path, monkeypatch, outcome, keep_state, expected
):
    sha = "2" * 40
    output = tmp_path / "report"
    captured = {}
    monkeypatch.setattr(workflow, "git", lambda *a, **kw: "" if a[0] == "status" else sha)
    monkeypatch.setattr(workflow.metadata, "distributions", lambda: [])

    def execute(command, *, env, stdout, **kwargs):
        state = Path(env["TERMINAL_MCP_DATABASE_PATH"]).parents[1]
        captured["state"] = state
        captured["command"] = command
        (state / "test.sqlite3").write_bytes(b"diagnostics")
        stdout.write("fake test output\n")
        (output / "junit.xml").write_text("<testsuites/>\n")
        if outcome == "timeout":
            raise workflow.subprocess.TimeoutExpired(command, 2)
        if outcome == "error":
            raise RuntimeError("unexpected runner failure")
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(workflow.subprocess, "run", execute)
    args = argparse.Namespace(
        expected_sha=sha,
        mode="dev",
        allow_dirty=False,
        output=str(output),
        timeout=2,
        sample=False,
        keep_state=keep_state,
        fresh_preparation=False,
    )
    plan = {"scope": "dev", "tests": ["tests/test_example.py"]}
    if outcome == "error":
        with pytest.raises(RuntimeError, match="unexpected runner failure"):
            workflow.run(args, plan, root=tmp_path)
        report = json.loads((output / "report.json").read_text())
        assert report["state"] == "error"
        assert report["error_type"] == "RuntimeError"
        assert report["full_test_evidence_eligible"] is False
        assert report["state_retained"] is False
    else:
        assert workflow.run(args, plan, root=tmp_path) == expected
        report = json.loads((output / "report.json").read_text())
        assert report["exit_code"] == expected
        assert report["state_retained"] is keep_state

    assert captured["state"].exists() is keep_state
    if keep_state:
        assert "tmp_path_retention_policy=all" in captured["command"]
        workflow.shutil.rmtree(captured["state"])
    else:
        assert "tmp_path_retention_policy=all" not in captured["command"]
    assert (output / "pytest.log").is_file()
