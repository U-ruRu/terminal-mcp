import os
import sys
import types
from pathlib import Path

import pytest

from terminal_mcp.config import Settings
from terminal_mcp.core.execution import ExecutionPortError
from terminal_mcp.deployment.split import CutoverError, SplitServiceSpec, rendered_files
from terminal_mcp.terminal.composition import build_execution


@pytest.fixture(autouse=True)
def isolate_instance_environment(monkeypatch):
    for key in list(os.environ):
        if key.startswith("TERMINAL_MCP_"):
            monkeypatch.delenv(key)


def test_factory_builds_the_configured_unix_adapter(monkeypatch):
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


def test_executor_service_allows_the_configured_api_user():
    files = rendered_files(SplitServiceSpec("terminal-mcp", 991), Settings(_env_file=None))

    executor = files[Path("/etc/systemd/system/terminal-mcp-executor.service")]
    assert "--allowed-uid 991" in executor


def test_root_controlled_directory_rejects_mutable_ancestor(tmp_path):
    from terminal_mcp.deployment.driver import check_root_controlled_paths

    root = tmp_path / "unsafe"
    root.mkdir(mode=0o777)
    root.chmod(0o777)
    with pytest.raises(CutoverError, match="unsafe_privileged_path"):
        check_root_controlled_paths([root / "client.env"])
