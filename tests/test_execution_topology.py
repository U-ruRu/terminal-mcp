import importlib
import json
import os
import sqlite3
import sys
import types
from pathlib import Path

import pytest
from pydantic import ValidationError

from terminal_mcp.config import Settings
from terminal_mcp.core.execution import ExecutionPortError
from terminal_mcp.deployment.split import (
    API_SERVICE,
    EXECUTOR_SERVICE,
    CutoverError,
    SplitServiceSpec,
    assert_drained,
    preflight,
    rendered_files,
)
from terminal_mcp.deployment.transition import TopologyTransition
from terminal_mcp.terminal.composition import build_execution
from terminal_mcp.terminal.in_process import InProcessExecutionAdapter


@pytest.fixture(autouse=True)
def isolate_instance_environment(monkeypatch):
    for key in list(os.environ):
        if key.startswith("TERMINAL_MCP_"):
            monkeypatch.delenv(key)


def test_default_preserves_in_process_compatibility():
    assert isinstance(build_execution(Settings(_env_file=None)), InProcessExecutionAdapter)


@pytest.mark.parametrize(
    "values",
    [
        {"execution_mode": "auto"},
        {"executor_socket_path": "relative.sock"},
        {"executor_socket_path": "/run/../tmp/test.sock"},
        {"executor_socket_path": "/run/\x00sock"},
        {"executor_socket_path": "/" + "x" * 108},
        {"executor_rpc_timeout_sec": float("nan")},
        {"executor_rpc_timeout_sec": float("inf")},
        {"executor_rpc_timeout_sec": 0},
        {"executor_rpc_timeout_sec": 301},
    ],
)
def test_invalid_topology_configuration_is_rejected(values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


def test_unix_factory_never_constructs_in_process(monkeypatch):
    fake = types.ModuleType("terminal_mcp.terminal.ipc")
    calls = []
    fake.UnixExecutionAdapter = lambda *args, **kwargs: calls.append((args, kwargs)) or "unix"
    monkeypatch.setitem(sys.modules, fake.__name__, fake)
    settings = Settings(_env_file=None, execution_mode="unix")
    assert build_execution(settings) == "unix"
    assert calls == [((Path("/run/terminal-mcp/executor.sock"),), {"rpc_timeout": 10.0})]


def test_missing_ipc_is_fail_closed(monkeypatch):
    monkeypatch.setitem(sys.modules, "terminal_mcp.terminal.ipc", None)
    with pytest.raises(ExecutionPortError, match="executor_adapter_unavailable"):
        build_execution(Settings(_env_file=None, execution_mode="unix"))


def test_unreachable_ipc_constructor_does_not_fall_back(monkeypatch):
    fake = types.ModuleType("terminal_mcp.terminal.ipc")

    def unavailable(*args, **kwargs):
        raise ConnectionRefusedError("not running")

    fake.UnixExecutionAdapter = unavailable
    monkeypatch.setitem(sys.modules, fake.__name__, fake)
    with pytest.raises(ConnectionRefusedError):
        build_execution(Settings(_env_file=None, execution_mode="unix"))


def test_bypassed_settings_validation_is_still_fail_closed():
    settings = Settings(_env_file=None).model_copy(update={"execution_mode": "auto"})
    with pytest.raises(ExecutionPortError, match="execution_mode_invalid"):
        build_execution(settings)


def test_app_composes_port_before_opening_any_store(monkeypatch):
    app_module = importlib.import_module("terminal_mcp.app")

    def fail(_settings):
        raise ExecutionPortError("executor_adapter_unavailable")

    monkeypatch.setattr(app_module, "build_execution", fail)

    def store_forbidden(*args, **kwargs):
        pytest.fail("store opened before execution composition validation")

    monkeypatch.setattr(app_module, "SqliteRepository", store_forbidden)
    with pytest.raises(ExecutionPortError):
        app_module.create_app(Settings(_env_file=None))


def test_units_restrict_caller_and_preserve_independent_lifetimes():
    files = rendered_files(SplitServiceSpec("terminal-mcp", 991), Settings(_env_file=None))
    executor = files[Path("/etc/systemd/system/terminal-mcp-executor.service")]
    api = files[Path("/etc/systemd/system/terminal-mcp.service.d/50-local-executor.conf")]
    assert "User=root" in executor and "Group=terminal-mcp" in executor
    assert "--allowed-uid 991" in executor
    assert "KillMode=control-group" in executor
    assert "RuntimeDirectoryMode=0750" in executor
    assert "User=terminal-mcp" in api and "NoNewPrivileges=true" in api
    assert "CapabilityBoundingSet=\n" in api
    assert "EnvironmentFile=/etc/terminal-mcp-executor/client.env" in api
    assert "PartOf=" not in executor + api and "BindsTo=" not in executor + api
    assert "--host" not in executor and "--port" not in executor
    assert len(files) == 3


@pytest.mark.parametrize(
    "kwargs",
    [
        {"api_uid": 0},
        {"api_uid": True},
        {"api_user": "root"},
        {"api_group": "root"},
        {"api_user": "name\nUser=root"},
        {"install_root": Path("/opt/%n")},
        {"install_root": Path("relative")},
    ],
)
def test_unsafe_service_spec_is_rejected(kwargs):
    values = {"api_user": "terminal-mcp", "api_uid": 991} | kwargs
    with pytest.raises(CutoverError):
        SplitServiceSpec(**values)


@pytest.fixture
def stores(tmp_path):
    runtime, auth = tmp_path / "runtime.db", tmp_path / "auth.db"
    with sqlite3.connect(runtime) as db:
        db.execute("CREATE TABLE commands (status TEXT NOT NULL)")
        db.execute("INSERT INTO commands VALUES ('completed')")
    with sqlite3.connect(auth) as db:
        db.execute("CREATE TABLE secrets (value TEXT)")
        db.execute("INSERT INTO secrets VALUES ('preserve-me')")
    return Settings(
        _env_file=None,
        database_path=runtime,
        auth_database_path=auth,
        output_cache_path=tmp_path / "output.db",
    )


def test_preflight_preserves_bytes_and_never_creates_optional_stores(stores):
    before = {p: p.read_bytes() for p in (stores.database_path, stores.auth_database_path)}
    result = preflight(stores)
    assert result["drained"] and result["database_backups_created"] == 0
    assert all(path.read_bytes() == content for path, content in before.items())
    assert not stores.output_cache_path.exists()
    assert not stores.effective_fleet_control_path().exists()


@pytest.mark.parametrize("status", ["queued", "running", "unknown"])
def test_cutover_refuses_undrained_queue(stores, status):
    with sqlite3.connect(stores.database_path) as db:
        db.execute("INSERT INTO commands VALUES (?)", (status,))
    with pytest.raises(CutoverError, match="commands_not_drained:1"):
        preflight(stores)


def test_corrupt_fleet_store_blocks_even_if_not_current_authority(stores):
    stores.effective_fleet_control_path().write_bytes(b"corruption")
    with pytest.raises(CutoverError, match="store_unreadable"):
        preflight(stores)


def test_missing_runtime_is_not_created(tmp_path):
    missing = tmp_path / "missing.db"
    with pytest.raises(CutoverError, match="required_store_missing"):
        assert_drained(missing)
    assert not missing.exists()


def test_low_disk_blocks_without_creating_backups(stores, monkeypatch):
    monkeypatch.setattr(
        "terminal_mcp.deployment.split.shutil.disk_usage",
        lambda _path: types.SimpleNamespace(free=1024),
    )
    with pytest.raises(CutoverError, match="insufficient_disk_space"):
        preflight(stores)


@pytest.fixture
def transition(tmp_path):
    files = {
        tmp_path / "executor.service": "new-executor",
        tmp_path / "api.conf": "new-api",
        tmp_path / "client.env": "unix",
    }
    files_path = tmp_path / "api.conf"
    files_path.write_text("old-api")
    files_path.chmod(0o640)
    events = []
    obj = TopologyTransition(
        files,
        tmp_path / "state/journal.json",
        control=lambda *args: events.append(args),
        check=lambda: events.append(("check",)),
        ready=lambda unit: events.append(("ready", unit)),
    )
    return obj, events


def test_activation_orders_readiness_and_is_idempotent(transition):
    obj, events = transition
    obj.activate()
    assert events[:3] == [("check",), ("stop", API_SERVICE), ("check",)]
    assert events.index(("ready", EXECUTOR_SERVICE)) < events.index(("start", API_SERVICE))
    assert json.loads(obj.journal.read_text())["phase"] == "active"
    before = list(events)
    obj.activate()
    assert events == before


def test_rollback_restores_exact_files_permissions_and_order(transition):
    obj, events = transition
    obj.activate()
    events.clear()
    obj.rollback()
    assert events[:4] == [("check",), ("stop", API_SERVICE), ("check",), ("stop", EXECUTOR_SERVICE)]
    paths = list(obj.files)
    assert not paths[0].exists() and not paths[2].exists()
    assert paths[1].read_text() == "old-api"
    assert paths[1].stat().st_mode & 0o777 == 0o640
    assert json.loads(obj.journal.read_text())["phase"] == "rolled_back"


def test_executor_start_failure_rolls_back_without_starting_split_api(transition):
    obj, events = transition

    def ready(unit):
        events.append(("ready", unit))
        if unit == EXECUTOR_SERVICE:
            raise RuntimeError("executor unavailable")

    obj.ready = ready
    with pytest.raises(RuntimeError, match="executor unavailable"):
        obj.activate()
    assert json.loads(obj.journal.read_text())["phase"] == "rolled_back"
    assert events.count(("start", API_SERVICE)) == 1  # restored compatibility API only


def test_rollback_never_kills_executor_when_work_appeared(transition):
    obj, events = transition
    obj.activate()
    events.clear()
    checks = iter([None, CutoverError("commands_not_drained:1")])

    def check():
        error = next(checks)
        if error:
            raise error

    obj.check = check
    with pytest.raises(CutoverError, match="rollback_blocked"):
        obj.rollback()
    assert ("stop", EXECUTOR_SERVICE) not in events
    assert json.loads(obj.journal.read_text())["phase"] == "rollback_blocked"
    assert all(p.read_text() == text for p, text in obj.files.items())


def test_process_restart_can_finish_rollback_from_durable_journal(transition):
    obj, events = transition
    obj.activate()
    record = json.loads(obj.journal.read_text())
    record["phase"] = "executor_ready"  # crash after files installed
    obj.journal.write_text(json.dumps(record))
    with pytest.raises(CutoverError, match="unfinished_cutover"):
        obj.activate()
    recovered = TopologyTransition(
        obj.files, obj.journal, control=obj.control, check=obj.check, ready=obj.ready
    )
    recovered.rollback()
    assert json.loads(obj.journal.read_text())["phase"] == "rolled_back"


def test_symlink_targets_are_refused_before_any_mutation(transition, tmp_path):
    obj, events = transition
    target = tmp_path / "outside"
    target.write_text("keep")
    path = next(iter(obj.files))
    path.symlink_to(target)
    with pytest.raises(CutoverError, match="symlink_target_refused"):
        obj.activate()
    assert target.read_text() == "keep"
    assert not any(event[0] == "stop" for event in events)


def test_release_change_blocks_rollback_before_services_stop(transition):
    obj, events = transition
    obj.identity = "release-one"
    obj.activate()
    events.clear()
    obj.identity = "release-two"
    with pytest.raises(CutoverError, match="release_identity_mismatch"):
        obj.rollback()
    assert events == []


def test_bad_snapshot_is_rejected_before_any_restore(transition):
    obj, events = transition
    obj.activate()
    record = json.loads(obj.journal.read_text())
    record["before"][str(list(obj.files)[-1])] = {"data": "not-base64", "mode": 420}
    obj.journal.write_text(json.dumps(record))
    events.clear()
    with pytest.raises(CutoverError, match="invalid_journal_data"):
        obj.rollback()
    assert events == []


def test_driver_executor_probe_runs_as_api_uid(monkeypatch):
    from terminal_mcp.deployment.driver import _executor_ready

    calls = []
    monkeypatch.setattr(
        "terminal_mcp.deployment.driver.subprocess.run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    _executor_ready(Settings(_env_file=None), SplitServiceSpec("terminal-mcp", 991))
    argv = calls[0][0][0]
    assert argv[:4] == ["runuser", "-u", "terminal-mcp", "--"]
    assert "await client.start()" in argv[-2]
    assert "await client.close()" in argv[-2]
    assert calls[0][1]["timeout"] == 5


def test_root_controlled_directory_rejects_mutable_ancestor(tmp_path):
    from terminal_mcp.deployment.driver import check_root_controlled_paths

    root = tmp_path / "unsafe"
    root.mkdir(mode=0o777)
    root.chmod(0o777)
    with pytest.raises(CutoverError, match="unsafe_privileged_path"):
        check_root_controlled_paths([root / "client.env"])


def test_mode_file_is_not_in_mutable_api_credentials_directory():
    spec = SplitServiceSpec("terminal-mcp", 991)
    paths = rendered_files(spec, Settings(_env_file=None))
    assert Path("/etc/terminal-mcp-executor/client.env") in paths
    assert all(not path.is_relative_to(spec.env_dir) for path in paths)


def test_legacy_installer_refuses_split_update_before_mutation(tmp_path):
    import subprocess

    unit = tmp_path / "terminal-mcp.service"
    dropin = Path(str(unit) + ".d")
    dropin.mkdir()
    (dropin / "50-local-executor.conf").write_text("split-enabled")
    root = tmp_path / "must-not-be-created"
    result = subprocess.run(
        ["bash", "deploy/install.sh", "update"],
        cwd=Path(__file__).resolve().parents[1],
        env={
            **os.environ,
            "TERMINAL_MCP_UNIT_FILE": str(unit),
            "TERMINAL_MCP_INSTALL_ROOT": str(root),
        },
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 46
    assert "Split executor topology is active" in result.stderr
    assert not root.exists()
