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
        "new-unknown-file.json",
        "tests/test_deleted.py",
    ],
)
def test_shared_unknown_or_deleted_changes_expand_component_feedback(
    tmp_path, monkeypatch, changed
):
    monkeypatch.setattr(test_plan, "COMPONENT_MODULES", frozenset({"test_a.py"}))
    write(tmp_path, "tests/test_a.py", "def test_a(): pass\n")
    plan = test_plan.affected_plan(tmp_path, [changed])
    assert plan["scope"] == "affected"
    assert plan["layer"] == "component"
    assert plan["tests"] == ["tests/test_a.py"]
    assert plan["reasons"]


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


def test_unresolved_relative_helper_import_expands_component_feedback(tmp_path, monkeypatch):
    monkeypatch.setattr(test_plan, "COMPONENT_MODULES", frozenset({"test_a.py", "test_b.py"}))
    write(tmp_path, "tests/test_a.py", "from .test_b import value\n")
    write(tmp_path, "tests/test_b.py", "value = 1\n")
    plan = test_plan.affected_plan(tmp_path, ["tests/test_b.py"])
    assert plan["scope"] == "affected"
    assert plan["tests"] == ["tests/test_a.py", "tests/test_b.py"]
    assert any("uncertain" in reason for reason in plan["reasons"])


def test_component_feedback_stays_with_direct_owner(tmp_path):
    for name in [
        "fleet_identity",
        "public_errors",
        "sqlite_posix_lock_preservation",
        "mcp_events_message_binding",
        "oauth_retention",
        "execution_ipc",
    ]:
        write(tmp_path, "tests/test_" + name + ".py")
    plan = test_plan.component_plan(tmp_path, ["mesh"])
    assert plan["components"] == ["mesh"]
    assert plan["tests"] == ["tests/test_fleet_identity.py"]


def test_integration_feedback_expands_declared_boundaries(tmp_path):
    for name in [
        "fleet_identity",
        "public_errors",
        "sqlite_posix_lock_preservation",
        "mcp_events_message_binding",
        "oauth_retention",
        "execution_ipc",
    ]:
        write(tmp_path, "tests/test_" + name + ".py")
    plan = test_plan.component_plan(tmp_path, ["mesh"], integration=True)
    assert set(plan["components"]) == {"auth", "contracts", "events", "mesh", "storage"}
    assert "tests/test_mcp_events_message_binding.py" in plan["tests"]
    assert "tests/test_oauth_retention.py" in plan["tests"]
    assert "tests/test_sqlite_posix_lock_preservation.py" in plan["tests"]
    assert "tests/test_execution_ipc.py" not in plan["tests"]


def test_component_selection_does_not_repeat_development_checks(tmp_path):
    write(tmp_path, "tests/test_public_errors.py")
    write(tmp_path, "tests/test_version.py")
    plan = test_plan.component_plan(tmp_path, ["contracts", "deployment"])
    assert plan["tests"] == ["tests/test_public_errors.py", "tests/test_version.py"]


def test_events_selection_keeps_storage_and_app_binding_separate(tmp_path):
    write(tmp_path, "tests/test_mcp_events_managed_storage.py")
    write(tmp_path, "tests/test_mcp_events_message_binding.py")
    component = test_plan.component_plan(tmp_path, ["events"])
    assert component["tests"] == ["tests/test_mcp_events_managed_storage.py"]
    assert component["deferred"]["integration"] == ["tests/test_mcp_events_message_binding.py"]
    integration = test_plan.component_plan(tmp_path, ["events"], integration=True)
    assert integration["tests"] == [
        "tests/test_mcp_events_managed_storage.py",
        "tests/test_mcp_events_message_binding.py",
    ]


def test_affected_selection_normalizes_native_paths_and_helper_consumers(tmp_path, monkeypatch):
    monkeypatch.setattr(
        test_plan,
        "COMPONENT_MODULES",
        frozenset(
            {
                "test_helper.py",
                "test_consumer.py",
            }
        ),
    )
    write(tmp_path, "tests/test_helper.py", "def helper(): pass\n")
    write(tmp_path, "tests/test_consumer.py", "from test_helper import helper\n")
    plan = test_plan.affected_plan(tmp_path, ["tests/test_helper.py", r"tests\test_helper.py"])
    assert plan["scope"] == "affected"
    assert plan["changed"] == ["tests/test_helper.py"]
    assert plan["tests"] == ["tests/test_consumer.py", "tests/test_helper.py"]


def test_documentation_change_selects_current_docs_without_global_runtime_checks(tmp_path):
    write(tmp_path, "tests/test_documentation_contract.py", "def test_docs(): pass\n")
    write(tmp_path, "tests/test_public_errors.py", "def test_errors(): pass\n")
    plan = test_plan.affected_plan(tmp_path, ["docs/testing.md"])
    assert plan["tests"] == ["tests/test_documentation_contract.py"]
    assert plan["deferred"] == {"integration": [], "platform": []}


def test_agent_instructions_select_docs_and_workflow_checks(tmp_path):
    write(tmp_path, "tests/test_documentation_contract.py")
    write(tmp_path, "tests/test_test_workflow.py")
    write(tmp_path, "tests/test_public_errors.py")
    plan = test_plan.affected_plan(tmp_path, ["AGENTS.md"])
    assert plan["tests"] == [
        "tests/test_documentation_contract.py",
        "tests/test_test_workflow.py",
    ]


def test_events_source_keeps_app_and_commit_boundaries_explicit(tmp_path):
    for name in [
        "mcp_events_managed_storage",
        "mcp_events_session_number",
        "mcp_events_message_binding",
        "mcp_events_message_binding",
        "access_mesh_shared_restart",
        "public_errors",
    ]:
        write(tmp_path, "tests/test_" + name + ".py")
    plan = test_plan.affected_plan(tmp_path, ["src/terminal_mcp/mcp/event_server.py"])
    assert plan["tests"] == ["tests/test_mcp_events_managed_storage.py"]
    assert "tests/test_mcp_events_session_number.py" in plan["deferred"]["integration"]
    assert "tests/test_mcp_events_message_binding.py" in plan["deferred"]["integration"]
    assert "tests/test_public_errors.py" not in plan["tests"]


@pytest.mark.parametrize(
    "path,kind,owners,boundaries",
    [
        (
            r"src\terminal_mcp\core\read_contract.py",
            "owner",
            ["contracts", "commands"],
            "consumers",
        ),
        ("src/terminal_mcp/http/new_route.py", "unknown", [], "all"),
    ],
)
def test_source_routes_expose_owner_or_safe_unknown(path, kind, owners, boundaries):
    route = test_plan.source_route(path)
    assert route["path"] == path.replace("\\", "/")
    assert route["kind"] == kind
    assert route["owners"] == owners
    assert route["boundaries"] == boundaries
    assert route["reason"]


def test_direct_source_imports_select_other_named_owner_and_helper_consumers(tmp_path, monkeypatch):
    monkeypatch.setattr(
        test_plan,
        "COMPONENT_MODULES",
        frozenset(
            {
                "test_read_contract.py",
                "test_other_owner.py",
                "test_indirect.py",
                "test_unrelated.py",
            }
        ),
    )
    source = "src/terminal_mcp/core/read_contract.py"
    write(tmp_path, source, "class ReadContract: pass\n")
    write(tmp_path, "tests/test_read_contract.py")
    write(
        tmp_path,
        "tests/conftest.py",
        "from terminal_mcp.core.read_contract import ReadContract\n",
    )
    write(tmp_path, "tests/test_other_owner.py", "from conftest import ReadContract\n")
    write(tmp_path, "tests/test_indirect.py", "from test_other_owner import ReadContract\n")
    write(tmp_path, "tests/test_unrelated.py")
    plan = test_plan.affected_plan(tmp_path, [source])
    assert plan["tests"] == [
        "tests/test_indirect.py",
        "tests/test_other_owner.py",
        "tests/test_read_contract.py",
    ]
    assert any("imports affected source" in reason for reason in plan["reasons"])
    integration = test_plan.affected_plan(tmp_path, [source], integration=True)
    assert integration["tests"] == plan["tests"]


def test_shared_store_changes_keep_direct_feedback_and_expose_all_boundaries(tmp_path):
    source = "src/terminal_mcp/storage/work_windows.py"
    write(tmp_path, source, "class WorkWindowStore: pass\n")
    write(
        tmp_path,
        "tests/test_work_window_store.py",
        "from terminal_mcp.storage.work_windows import WorkWindowStore\n",
    )
    write(tmp_path, "tests/test_task_storage.py")
    write(tmp_path, "tests/test_read_contract.py")
    write(tmp_path, "tests/test_execution_port_native.py")
    plan = test_plan.affected_plan(tmp_path, [source])
    assert plan["tests"] == ["tests/test_task_storage.py"]
    assert "tests/test_work_window_store.py" in plan["deferred"]["integration"]
    assert "tests/test_read_contract.py" in plan["deferred"]["integration"]
    assert plan["deferred"]["platform"] == ["tests/test_execution_port_native.py"]
    explicit = test_plan.affected_plan(tmp_path, [source], integration=True)
    assert "tests/test_work_window_store.py" in explicit["tests"]
    assert "tests/test_execution_port_native.py" not in explicit["tests"]


def test_source_contract_boundaries_include_consuming_owners(tmp_path):
    write(tmp_path, "tests/test_mcp_events_managed_storage.py")
    write(tmp_path, "tests/test_work_window_domain.py")
    plan = test_plan.affected_plan(tmp_path, ["src/terminal_mcp/mcp/event_server.py"])
    assert plan["tests"] == ["tests/test_mcp_events_managed_storage.py"]
    assert "tests/test_work_window_domain.py" in plan["deferred"]["integration"]


def test_current_source_inventory_has_visible_routes_and_no_missing_test_layer():
    root = SCRIPTS.parent
    for source in (root / "src").rglob("*"):
        if source.is_file() and "__pycache__" not in source.parts:
            route = test_plan.source_route(source.relative_to(root).as_posix())
            assert route["kind"] in {"owner", "unknown"}
            assert route["owners"] or route["boundaries"] == "all"
    files = test_plan.test_files(root)
    index = {relative: test_plan.test_functions(root, relative) for relative in files}

    def functions(selectors):
        return {
            relative + "::" + function
            for selector in selectors
            for relative in [selector.split("::")[0]]
            for function in index[relative]
            if "::" not in selector or relative + "::" + function == selector
        }

    component = functions(test_plan.component_plan(root, ["all"])["tests"])
    integration = functions(test_plan.component_plan(root, ["all"], integration=True)["tests"])
    platform = functions(test_plan.platform_plan(root, ["linux"])["tests"])
    assert not platform & (component | integration)
    assert component <= integration
    assert integration | platform == functions(files)


def test_platform_tests_cannot_enter_component_or_integration_feedback(tmp_path):
    write(tmp_path, "tests/test_read_contract.py")
    write(tmp_path, "tests/test_execution_port_native.py")
    write(tmp_path, "qa/windows/test_execution.py")
    assert test_plan.component_plan(tmp_path, ["commands"])["tests"] == [
        "tests/test_read_contract.py",
    ]
    assert test_plan.component_plan(tmp_path, ["commands"], integration=True)["tests"] == [
        "tests/test_read_contract.py",
    ]
    assert test_plan.platform_plan(tmp_path, ["linux"])["tests"] == [
        "tests/test_execution_port_native.py",
    ]
    assert test_plan.platform_plan(tmp_path, ["windows"])["tests"] == [
        "qa/windows/test_execution.py",
    ]


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


def test_stale_layer_declaration_is_blocked(tmp_path):
    write(tmp_path, "tests/test_compact_contracts.py", "def test_new_name(): pass\n")
    with pytest.raises(ValueError, match="Stale test-layer declaration"):
        test_plan.component_plan(tmp_path, ["contracts"])


def test_empty_component_selection_never_falls_back_to_pytest_default_suite(tmp_path, monkeypatch):
    write(tmp_path, "tests/test_mcp_events_unreviewed.py")
    monkeypatch.setattr(workflow, "ROOT", tmp_path)
    monkeypatch.setattr(workflow, "run", lambda *args, **kwargs: pytest.fail("must not run pytest"))
    assert workflow.main(["component", "events"]) == 0


def test_run_refuses_empty_selection_before_reading_source_identity(tmp_path):
    with pytest.raises(ValueError, match="Empty test selection"):
        workflow.run(argparse.Namespace(), {"tests": []}, root=tmp_path)


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
    assert a["TMPDIR"] == str(first)
    assert b["TMPDIR"] == str(second)
    assert all(a[name] == str(first) for name in ("TMP", "TEMP"))


@pytest.mark.parametrize(
    "argv",
    [
        ["full", "--changed-from", "HEAD", "--plan"],
        ["component", "invalid", "--plan"],
        ["node"],
        ["dev", "--timeout", "0", "--plan"],
        ["platform", "darwin", "--plan"],
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
    assert receipt["source_sha"] == sha
    assert receipt["runtime"]["sqlite_version"]
    assert receipt["runtime"]["python_version"]
    assert receipt["full_test_evidence_eligible"] is full_test_evidence_eligible
    assert receipt["dirty"] is bool(dirty)
    assert len(receipt["files"]["junit.xml"]) == 64
    assert receipt["tests"]["collected"] == 1
    assert receipt["state_retained"] is False
    assert not Path(receipt["state_dir"]).exists()


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


def test_development_checks_cover_generated_contract_provenance():
    assert "tests/test_public_errors.py" in test_plan.DEV_TESTS
    assert "tests/test_compact_contracts.py" in test_plan.DEV_TESTS
