import asyncio
import os
import pwd
import signal
import time
from datetime import UTC, datetime


class LinuxTerminalAdapter:
    def __init__(
        self,
        repo,
        shell,
        cwd,
        grace,
        user="root",
        queue_workers=4,
        queue_reconcile_sec=1.0,
    ):
        self.repo = repo
        self.shell = shell
        self.cwd = cwd
        self.grace = grace
        self.user = user
        self.queue_workers = max(1, int(queue_workers))
        self.queue_reconcile_sec = max(0.05, float(queue_reconcile_sec))
        self.processes = {}
        self.process_queues = {}
        self.capture_processes = set()
        self.queue_events = {
            queue_id: asyncio.Event() for queue_id in range(1, self.queue_workers + 1)
        }
        self.workers = {}
        self.cancel_requested = set()
        self.stopping = False
        self.queue = ()  # compatibility surface; SQLite is the queue source of truth.

    async def start(self):
        self.stopping = False
        for queue_id in range(1, self.queue_workers + 1):
            worker = self.workers.get(queue_id)
            if worker is None or worker.done():
                self.workers[queue_id] = asyncio.create_task(
                    self._worker(queue_id), name=f"terminal-worker-q{queue_id}"
                )

    async def stop(self):
        self.stopping = True
        processes = [*self.processes.values(), *self.capture_processes]
        for process in processes:
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGTERM)
        if processes:
            await asyncio.gather(*(process.wait() for process in processes), return_exceptions=True)
            await asyncio.sleep(0)
        workers = list(self.workers.values())
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self.workers.clear()

    def _valid_queue(self, queue_id):
        return isinstance(queue_id, int) and 1 <= queue_id <= self.queue_workers

    async def submit(self, command):
        if not self._valid_queue(command.queue_id):
            raise ValueError(
                f"queue_id must be between 1 and {self.queue_workers}, got {command.queue_id!r}"
            )
        if not self.workers or any(worker.done() for worker in self.workers.values()):
            await self.start()
        self.queue_events[command.queue_id].set()

    async def discard_queued(self, cmd_hash):
        command = await self.repo.get(cmd_hash)
        if command is None or command.status != "queued":
            return False
        removed = await self.repo.cancel_queued(cmd_hash)
        if removed and command.queue_id in self.queue_events:
            self.queue_events[command.queue_id].set()
        return removed

    async def _wait_for_work(self, queue_id):
        event = self.queue_events[queue_id]
        event.clear()
        try:
            await asyncio.wait_for(event.wait(), self.queue_reconcile_sec)
        except TimeoutError:
            pass

    async def _worker(self, queue_id):
        while not self.stopping:
            command = await self.repo.claim_next(queue_id)
            if command is None:
                await self._wait_for_work(queue_id)
                continue
            try:
                await self._execute(command, method="run")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.repo.finish_running(
                    command.cmd_hash, "failed", command.exit_code, f"run.worker: {exc}"
                )

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
        batch = []
        batch_bytes = 0
        discarding_long_line = False
        accepting = True
        overflow_marked = False
        line_limit = max(1, int(getattr(self.repo, "output_line_max_bytes", 4 * 1024 * 1024)))

        async def flush():
            nonlocal batch, batch_bytes, accepting
            if not batch or not accepting:
                batch = []
                batch_bytes = 0
                return
            result = await self.repo.append_lines(command.cmd_hash, batch)
            accepting = bool(result.get("accepting", True))
            batch = []
            batch_bytes = 0

        async def add_raw(raw):
            nonlocal batch_bytes
            if not accepting:
                return
            text = raw.rstrip(b"\r").decode(errors="replace")
            batch.append(text)
            batch_bytes += len(raw)
            if len(batch) >= 64 or batch_bytes >= 256 * 1024:
                await flush()

        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            if not accepting:
                if not overflow_marked:
                    await self.repo.mark_output_truncated(command.cmd_hash)
                    overflow_marked = True
                continue
            data = chunk
            while data:
                if discarding_long_line:
                    newline = data.find(b"\n")
                    if newline < 0:
                        data = b""
                        continue
                    data = data[newline + 1 :]
                    discarding_long_line = False
                    continue

                newline = data.find(b"\n")
                if newline >= 0:
                    raw = pending + data[:newline]
                    pending = b""
                    data = data[newline + 1 :]
                    await add_raw(raw)
                    continue

                if len(pending) + len(data) > line_limit:
                    # Keep one byte beyond the configured limit so OutputStore records
                    # the line as truncated, then discard the rest until the newline.
                    take = max(0, line_limit + 1 - len(pending))
                    raw = pending + data[:take]
                    pending = b""
                    data = data[take:]
                    await add_raw(raw)
                    discarding_long_line = True
                    continue

                pending += data
                data = b""

        if accepting and pending and not discarding_long_line:
            await add_raw(pending)
        await flush()

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
        current = await self.repo.get(command.cmd_hash)
        if current is None or current.status != "running":
            return round((time.monotonic() - started) * 1000)
        command = current
        if command.cmd_hash in self.cancel_requested:
            await self.repo.finish_running(command.cmd_hash, "cancelled")
            self.cancel_requested.discard(command.cmd_hash)
            return round((time.monotonic() - started) * 1000)

        process = None
        pipe_task = None
        error = None
        final_status = None
        try:
            process = await self._spawn()
            self.processes[command.cmd_hash] = process
            self.process_queues[command.cmd_hash] = command.queue_id
            command.pid = process.pid
            if not await self.repo.set_pid(command.cmd_hash, process.pid):
                await self._terminate(process, grace_seconds=0.1)
                return round((time.monotonic() - started) * 1000)
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
                error = f"{method}.timeout: command exceeded {round(timeout_seconds * 1000)} ms"
                self.cancel_requested.add(command.cmd_hash)
                await self._terminate(process, grace_seconds=0.5)
                await execution

            command.exit_code = process.returncode
            if command.cmd_hash in self.cancel_requested:
                final_status = "cancelled"
            else:
                final_status = "completed" if process.returncode == 0 else "failed"
        except asyncio.CancelledError:
            if process and process.returncode is None:
                self.cancel_requested.add(command.cmd_hash)
                await self._terminate(process, grace_seconds=0.5)
                if pipe_task and not pipe_task.done():
                    await asyncio.gather(pipe_task, return_exceptions=True)
                command.exit_code = process.returncode
            final_status = "cancelled"
            error = f"{method}.cancelled: upstream disconnected"
            await self.repo.finish_running(command.cmd_hash, final_status, command.exit_code, error)
            raise
        except Exception as exc:
            final_status = "failed"
            error = f"{method}.execute: {exc}"
            if process and process.returncode is None:
                await self._terminate(process, grace_seconds=0.5)
                command.exit_code = process.returncode
            if pipe_task and not pipe_task.done():
                pipe_task.cancel()
        finally:
            self.processes.pop(command.cmd_hash, None)
            self.process_queues.pop(command.cmd_hash, None)
            if final_status is not None:
                await self.repo.finish_running(
                    command.cmd_hash, final_status, command.exit_code, error
                )
                await self.repo.prune_output_cache()
            self.cancel_requested.discard(command.cmd_hash)
            if command.queue_id in self.queue_events:
                self.queue_events[command.queue_id].set()
        return round((time.monotonic() - started) * 1000)

    async def recovery(self, command, timeout_seconds=20):
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
        timestamp = datetime.now(UTC).strftime("%H:%M:%S")
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

    async def cancel(self, command, timeout_seconds=10):
        started = time.monotonic()
        deadline = started + timeout_seconds
        if command.status == "queued":
            if await self.repo.cancel_queued(command.cmd_hash):
                if command.queue_id in self.queue_events:
                    self.queue_events[command.queue_id].set()
                return True, None
            command = await self.repo.get(command.cmd_hash)
            if command is None:
                return False, "cancel.lookup: command not found"

        if command.status not in {"queued", "running"}:
            return False, f"cancel.state: command is already {command.status}"

        if command.status == "queued":
            if await self.repo.cancel_queued(command.cmd_hash):
                return True, None
            command = await self.repo.get(command.cmd_hash) or command

        if command.status != "running":
            return command.status == "cancelled", (
                None
                if command.status == "cancelled"
                else f"cancel.state: command is already {command.status}"
            )

        self.cancel_requested.add(command.cmd_hash)
        process = self.processes.get(command.cmd_hash)
        while process is None and time.monotonic() < deadline:
            refreshed = await self.repo.get(command.cmd_hash)
            if refreshed is None:
                self.cancel_requested.discard(command.cmd_hash)
                return False, "cancel.lookup: command not found"
            if refreshed.status == "cancelled":
                self.cancel_requested.discard(command.cmd_hash)
                return True, None
            if refreshed.status not in {"queued", "running"}:
                self.cancel_requested.discard(command.cmd_hash)
                return False, f"cancel.state: command is already {refreshed.status}"
            process = self.processes.get(command.cmd_hash)
            if process is None:
                await asyncio.sleep(0.01)

        if process is None:
            changed = await self.repo.finish_running(command.cmd_hash, "cancelled")
            self.cancel_requested.discard(command.cmd_hash)
            return (
                (True, None) if changed else (False, "cancel.wait_process: command changed state")
            )

        if process.returncode is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                term_wait = min(5.0, max(0.0, timeout_seconds - 0.5))
                await asyncio.wait_for(process.wait(), term_wait)
            except TimeoutError:
                os.killpg(process.pid, signal.SIGKILL)
                try:
                    kill_wait = min(3.0, max(0.0, deadline - time.monotonic()))
                    await asyncio.wait_for(process.wait(), kill_wait)
                except TimeoutError:
                    self.cancel_requested.discard(command.cmd_hash)
                    return False, (
                        "cancel.wait_process: process did not stop within "
                        f"{round(timeout_seconds * 1000)} ms"
                    )

        await self.repo.finish_running(command.cmd_hash, "cancelled", process.returncode, None)
        current = await self.repo.get(command.cmd_hash)
        return (current is not None and current.status == "cancelled"), None

    async def least_loaded_queue(self):
        loads = await self.repo.queue_loads(self.queue_workers)
        return min(loads, key=lambda queue_id: (loads[queue_id], queue_id))

    async def health(self):
        uid = os.geteuid()
        queues = await self.repo.queue_snapshot(self.queue_workers)
        running = [item["running"] for item in queues if item["running"]]
        output_cache = await self.repo.output_cache_stats()
        return {
            "ok": bool(self.workers) and all(not worker.done() for worker in self.workers.values()),
            "user": pwd.getpwuid(uid).pw_name,
            "uid": uid,
            "gid": os.getegid(),
            "cwd": str(self.cwd),
            "privilege": "root" if uid == 0 else "user",
            "shell": self.shell,
            "terminal_user": self.user,
            "scheduler": "numbered-fifo",
            "parallelism": self.queue_workers,
            "queue_size": sum(item["queued"] for item in queues),
            "running_commands": running,
            "queues": queues,
            "worker_health": {
                str(queue_id): not worker.done() for queue_id, worker in self.workers.items()
            },
            "output_cache": output_cache,
        }
