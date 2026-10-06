"""Composition of the execution port; never a transport-to-shell fallback.

The compatibility topology remains explicit and is the migration default. Selecting
Unix IPC must fail closed if the executor is missing, incompatible or unreachable.
The application scheduler owns the lifecycle and durable queue in both topologies.
"""

from terminal_mcp.config import Settings
from terminal_mcp.core.execution import ExecutionPort, ExecutionPortError


def build_execution(settings: Settings) -> ExecutionPort:
    if settings.execution_mode == "unix":
        try:
            from terminal_mcp.terminal.ipc import UnixExecutionAdapter
        except ImportError as exc:
            raise ExecutionPortError("executor_adapter_unavailable") from exc
        return UnixExecutionAdapter(
            settings.executor_socket_path, rpc_timeout=settings.executor_rpc_timeout_sec
        )
    if settings.execution_mode != "in_process":
        # Defend even Settings.model_construct()/model_copy() callers bypassing validation.
        raise ExecutionPortError("execution_mode_invalid")
    from terminal_mcp.terminal.in_process import InProcessExecutionAdapter

    return InProcessExecutionAdapter(
        settings.shell, settings.cwd, settings.cancel_grace_sec, settings.terminal_user
    )
