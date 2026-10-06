"""Compatibility composition; application state and process execution are separate."""

from terminal_mcp.application.command_scheduler import CommandScheduler
from terminal_mcp.terminal.in_process import InProcessExecutionAdapter


class LinuxTerminalAdapter(CommandScheduler):
    def __init__(
        self, repo, shell, cwd, grace, user="root", queue_workers=4, queue_reconcile_sec=1.0
    ):
        execution = InProcessExecutionAdapter(shell, cwd, grace, user)
        super().__init__(repo, execution, grace, queue_workers, queue_reconcile_sec)

    @property
    def output_readers(self):
        return self.execution.output_readers

    @property
    def output_transports(self):
        return self.execution.output_transports
