"""PowerShell ExecutionPort with command admission before input and owned Job Objects."""

from __future__ import annotations

import asyncio
import subprocess

import win32api
import win32con
import win32job

from terminal_mcp.core.execution import ExecutionPortError
from terminal_mcp.platform_windows import identity, is_system, process_exists, validate_service_user
from terminal_mcp.terminal.in_process import InProcessExecutionAdapter

# The trusted bootstrap waits for EOF. User code arrives after Job assignment and
# the scheduler's durable PID admission fence. UTF-8 applies to scripts and output.
_BOOTSTRAP = (
    "$utf8 = New-Object System.Text.UTF8Encoding($false); "
    "[Console]::InputEncoding = $utf8; [Console]::OutputEncoding = $utf8; "
    "$OutputEncoding = $utf8; "
    "$ErrorActionPreference = 'Stop'; "
    "$script = [Console]::In.ReadToEnd(); "
    "try { & ([ScriptBlock]::Create($script)); $succeeded = $?; "
    "if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }; "
    "if ($succeeded) { exit 0 } else { exit 1 } } catch { "
    "[Console]::Error.WriteLine($_.ToString()); exit 1 }"
)


class WindowsExecutionAdapter(InProcessExecutionAdapter):
    def __init__(self, shell, cwd, grace, user="current"):
        validate_service_user(user)
        super().__init__(shell, cwd, grace, user)
        self._jobs = {}

    async def _spawn_native(self, *, capture=False):
        job = win32job.CreateJobObject(None, "")
        process = None
        try:
            limits = win32job.QueryInformationJobObject(
                job, win32job.JobObjectExtendedLimitInformation
            )
            limits["BasicLimitInformation"]["LimitFlags"] |= (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            win32job.SetInformationJobObject(
                job, win32job.JobObjectExtendedLimitInformation, limits
            )
            process = await asyncio.create_subprocess_exec(
                self.shell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _BOOTSTRAP,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=self.cwd,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            native = win32api.OpenProcess(
                win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, process.pid
            )
            try:
                win32job.AssignProcessToJobObject(job, native)
            finally:
                native.Close()
            self._jobs[process.pid] = job
            if not capture:
                self.output_readers[process.pid] = process.stdout
            return process
        except BaseException:
            if process is not None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
            job.Close()
            raise

    async def signal(self, handle, action):
        process = self._entry(handle).process
        if action not in {"terminate", "kill"}:
            raise ExecutionPortError("execution_signal_invalid")
        # A root may have exited while descendants still hold the output pipe.
        self._stop_job(process)

    def _stop_job(self, process):
        job = self._jobs.get(process.pid)
        if job is not None:
            win32job.TerminateJobObject(job, 1)

    async def _terminate_native(self, process, grace_seconds=1.0):
        self._stop_job(process)
        try:
            await self._wait_root_exit(process, max(1.0, grace_seconds))
            return True
        except TimeoutError:
            return False

    def _release_output_stream(self, process):
        self._release_job(process)
        self.output_readers.pop(process.pid, None)
        # Closing the subprocess transport releases inherited output handles even
        # when a descendant retained them. The root returncode is already observed.
        process._transport.close()

    def _release_job(self, process):
        job = self._jobs.pop(process.pid, None)
        if job is not None:
            job.Close()

    def _kill_capture(self, process):
        self._stop_job(process)

    def _release_capture(self, process):
        self._release_job(process)
        process._transport.close()

    @staticmethod
    def _pid_exists(pid):
        return process_exists(pid)

    async def health(self):
        _sid, user = identity()
        return {
            "user": user,
            "uid": -1,
            "gid": -1,
            "cwd": str(self.cwd),
            "privilege": "system" if is_system() else "user",
            "shell": self.shell,
            "terminal_user": self.user,
        }
