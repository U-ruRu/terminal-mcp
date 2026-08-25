import asyncio
import os
import pwd
import signal
import time
from collections import deque
from datetime import UTC, datetime


class LinuxTerminalAdapter:
    def __init__(self, repo, shell, cwd, grace, user="root"):
        self.repo = repo
        self.shell = shell
        self.cwd = cwd
        self.grace = grace
        self.user = user
        self.processes = {}
        self.capture_processes = set()
        self.queue = deque()
        self.queue_event = asyncio.Event()
        self.cancel_requested = set()
        self.worker = None
        self.stopping = False

    async def start(self):
        if self.worker is None or self.worker.done():
            self.stopping = False
            self.worker = asyncio.create_task(self._worker(), name="terminal-worker")

    async def stop(self):
        self.stopping = True
        processes = [*self.processes.values(), *self.capture_processes]
        for process in processes:
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGTERM)
        if processes:
            await asyncio.gather(*(process.wait() for process in processes), return_exceptions=True)
            await asyncio.sleep(0)
        if self.worker:
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass
            self.worker = None

    async def submit(self, command):
        if self.worker is None or self.worker.done():
            await self.start()
        self.queue.append(command)
        self.queue_event.set()

    async def discard_queued(self, cmd_hash):
        for command in self.queue:
            if command.cmd_hash == cmd_hash:
                self.queue.remove(command)
                if not self.queue:
                    self.queue_event.clear()
                return True
        return False

    async def _next_command(self):
        while True:
            if self.queue:
                command = self.queue.popleft()
                if not self.queue:
                    self.queue_event.clear()
                return command
            self.queue_event.clear()
            await self.queue_event.wait()

    async def _worker(self):
        while not self.stopping:
            command = await self._next_command()
            if self.stopping:
                return
            try:
                current = await self.repo.get(command.cmd_hash)
                if current and current.status == "queued":
                    await self._execute(current, method="run")
            except Exception as exc:
                command.status = "failed"
                command.error = f"run.worker: {exc}"
                try:
                    await self.repo.update(command)
                except Exception:
                    pass

    def _drop_privileges(self):
        account = pwd.getpwnam(self.user)

        def drop_privileges():
            if os.geteuid() == 0:
                os.initgroups(account.pw_name, account.pw_gid)
                os.setgid(account.pw_gid)
                os.setuid(account.pw_uid)

        return drop_privileges

    async def _spawn(self):
        return await asyncio.create_subprocess_exec(
            self.shell,
            "-s",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=self.cwd,
            start_new_session=True,
            preexec_fn=self._drop_privileges(),
        )

    async def _pipe_output(self, command, reader):
        pending = b""
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            pending += chunk
            while b"\n" in pending:
                raw, pending = pending.split(b"\n", 1)
                await self.repo.append_line(
                    command.cmd_hash,
                    raw.rstrip(b"\r").decode(errors="replace"),
                )
        if pending:
            await self.repo.append_line(
                command.cmd_hash,
                pending.rstrip(b"\r").decode(errors="replace"),
            )

    async def _terminate(self, process, grace_seconds=1.0):
        if process.returncode is not None:
            return True
        os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), max(0.0, grace_seconds))
            return True
        except TimeoutError:
            os.killpg(process.pid, signal.SIGKILL)
            try:
                await asyncio.wait_for(process.wait(), 1.0)
                return True
            except TimeoutError:
                return False

    async def _execute(self, command, *, method, timeout_seconds=None):
        started = time.monotonic()
        command.status = "running"
        command.error = None
        await self.repo.update(command)
        if command.cmd_hash in self.cancel_requested:
            command.status = "cancelled"
            await self.repo.update(command)
            self.cancel_requested.discard(command.cmd_hash)
            return round((time.monotonic() - started) * 1000)

        process = None
        pipe_task = None
        try:
            process = await self._spawn()
            self.processes[command.cmd_hash] = process
            command.pid = process.pid
            await self.repo.update(command)
            process.stdin.write(command.cmd.encode())
            await process.stdin.drain()
            process.stdin.close()
            pipe_task = asyncio.create_task(self._pipe_output(command, process.stdout))
            wait_task = asyncio.create_task(process.wait())
            execution = asyncio.gather(pipe_task, wait_task)
            try:
                if timeout_seconds is None:
                    await execution
                else:
                    await asyncio.wait_for(asyncio.shield(execution), timeout_seconds)
            except TimeoutError:
                command.error = (
                    f"{method}.timeout: command exceeded {round(timeout_seconds * 1000)} ms"
                )
                self.cancel_requested.add(command.cmd_hash)
                await self._terminate(process, grace_seconds=0.5)
                await execution

            command.exit_code = process.returncode
            if command.cmd_hash in self.cancel_requested:
                command.status = "cancelled"
            else:
                command.status = "completed" if process.returncode == 0 else "failed"
        except Exception as exc:
            command.status = "failed"
            command.error = f"{method}.execute: {exc}"
            if process and process.returncode is None:
                await self._terminate(process, grace_seconds=0.5)
                command.exit_code = process.returncode
            if pipe_task and not pipe_task.done():
                pipe_task.cancel()
        finally:
            self.processes.pop(command.cmd_hash, None)
            await self.repo.update(command)
            self.cancel_requested.discard(command.cmd_hash)
        return round((time.monotonic() - started) * 1000)

    async def recovery(self, command, timeout_seconds=45):
        return await self._execute(
            command,
            method="recovery",
            timeout_seconds=timeout_seconds,
        )

    async def capture(
        self, command, timeout_ms=5000, max_output_lines=1000, timeout_error="capture timed out"
    ):
        started = time.monotonic()
        process = await self._spawn()
        self.capture_processes.add(process)
        communication = asyncio.create_task(process.communicate(command.encode()))
        error = None
        try:
            output, _ = await asyncio.wait_for(
                asyncio.shield(communication), max(0, timeout_ms) / 1000
            )
        except TimeoutError:
            error = timeout_error
            os.killpg(process.pid, signal.SIGKILL)
            output, _ = await communication
        finally:
            self.capture_processes.discard(process)
        status = "completed" if process.returncode == 0 and error is None else "failed"
        timestamp = datetime.now(UTC).strftime("%H:%M:%SZ")
        lines = [
            f"[{timestamp}] {line.rstrip(chr(13))}"
            for line in output.decode(errors="replace").splitlines()[:max_output_lines]
        ]
        return {
            "lines": lines,
            "status": status,
            "exit_code": process.returncode,
            "error": error,
            "ok": status == "completed",
            "duration_ms": round((time.monotonic() - started) * 1000),
        }

    async def cancel(self, command, timeout_seconds=45):
        started = time.monotonic()
        deadline = started + timeout_seconds
        if command.status == "queued":
            removed = await self.discard_queued(command.cmd_hash)
            if removed:
                command.status = "cancelled"
                command.error = None
                await self.repo.update(command)
                return True, None
            command = await self.repo.get(command.cmd_hash)
            if command is None:
                return False, "cancel.lookup: command not found"

        if command.status not in {"queued", "running"}:
            return False, f"cancel.state: command is already {command.status}"

        self.cancel_requested.add(command.cmd_hash)
        process = self.processes.get(command.cmd_hash)
        while process is None and time.monotonic() < deadline:
            refreshed = await self.repo.get(command.cmd_hash)
            if refreshed is None:
                self.cancel_requested.discard(command.cmd_hash)
                return False, "cancel.lookup: command not found"
            if refreshed.status == "cancelled":
                return True, None
            if refreshed.status not in {"queued", "running"}:
                self.cancel_requested.discard(command.cmd_hash)
                return False, f"cancel.state: command is already {refreshed.status}"
            process = self.processes.get(command.cmd_hash)
            if process is None:
                await asyncio.sleep(0.01)

        if process is None:
            self.cancel_requested.discard(command.cmd_hash)
            return False, "cancel.wait_process: process did not become available within 45000 ms"

        remaining = max(0.0, deadline - time.monotonic())
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), max(0.0, remaining - 0.5))
            except TimeoutError:
                os.killpg(process.pid, signal.SIGKILL)
                try:
                    await asyncio.wait_for(process.wait(), max(0.0, deadline - time.monotonic()))
                except TimeoutError:
                    self.cancel_requested.discard(command.cmd_hash)
                    return False, "cancel.wait_process: process did not stop within 45000 ms"

        command = await self.repo.get(command.cmd_hash) or command
        command.status = "cancelled"
        command.exit_code = process.returncode
        command.error = None
        await self.repo.update(command)
        return True, None

    async def health(self):
        uid = os.geteuid()
        return {
            "ok": self.worker is not None and not self.worker.done(),
            "user": pwd.getpwuid(uid).pw_name,
            "uid": uid,
            "gid": os.getegid(),
            "cwd": str(self.cwd),
            "privilege": "root" if uid == 0 else "user",
            "shell": self.shell,
            "terminal_user": self.user,
            "scheduler": "fifo",
            "parallelism": 1,
            "queue_size": len(self.queue),
            "running_commands": list(self.processes),
        }
