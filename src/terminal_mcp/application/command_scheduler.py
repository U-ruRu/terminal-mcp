"""Application-owned durable FIFO scheduler over a replaceable ExecutionPort.

Only this layer claims queues, fences command input and persists output/status.
The process adapter has no repository and cannot replay durable work on its own.
"""

from __future__ import annotations

import asyncio
import secrets
import threading
import time

from terminal_mcp.core.execution import (
    EXECUTION_PORT_VERSION,
    MAX_EXECUTION_READ_BYTES,
    CommandExecutionSnapshot,
    ExecutionPort,
    ExecutionPortError,
    ExecutionRequest,
)


class CommandScheduler:
    def __init__(
        self, repo, execution: ExecutionPort, grace, queue_workers=4, queue_reconcile_sec=1.0
    ):
        if execution.version != EXECUTION_PORT_VERSION:
            raise ExecutionPortError("execution_version_unsupported")
        self.repo, self.execution, self.grace = repo, execution, grace
        self.queue_workers = max(1, int(queue_workers))
        self.queue_reconcile_sec = max(0.05, float(queue_reconcile_sec))
        self.processes = {}
        self.process_queues = {}
        self.queue_events = {q: asyncio.Event() for q in range(1, self.queue_workers + 1)}
        self.workers = {}
        self.cancel_requested = set()
        self.execution_done = {}
        self.finalization_pending = {}
        self.reconciler_task = None
        self.claimed_commands = set()
        self.claimed_commands_lock = threading.Lock()
        self.pidless_first_seen = {}
        self.initial_pidless_reconciled = False
        self.stopping = False
        self.queue = ()  # Compatibility only: the repository owns durable FIFO.

    async def _spawn(self, *, execution_id=None):
        return await self.execution.spawn(ExecutionRequest(execution_id or secrets.token_hex(16)))

    async def _pid_exists(self, pid):
        return await self.execution.process_exists(pid)

    async def process_exists(self, pid):
        return await self._pid_exists(pid)

    def owns_command(self, command_hash):
        return command_hash in self.processes

    def owns_pending(self, command):
        return self._owns_pidless_running(command)

    def cancel_pending(self, command_hash):
        self.cancel_requested.add(command_hash)

    async def _wait_root_exit(self, handle, timeout_seconds=None):
        return await self.execution.wait(handle, timeout_seconds)

    async def _terminate(self, handle, grace_seconds=1.0):
        return await self.execution.terminate(handle, grace_seconds)

    async def _release_output_stream(self, handle):
        if handle is not None:
            await self.execution.release(handle)

    async def capture(
        self, command, timeout_ms=5000, max_output_lines=1000, timeout_error="capture timed out"
    ):
        return await self.execution.capture(command, timeout_ms, max_output_lines, timeout_error)

    async def snapshot(self, command_hash):
        command = await self.repo.get(command_hash)
        if command is None:
            return None
        return CommandExecutionSnapshot(
            command.cmd_hash,
            command.status,
            command.queue_id,
            await self.repo.queue_position(command.cmd_hash),
            bool(command.claimed_at is not None or command.started_at is not None),
            command.exit_code,
        )

    async def start(self):
        await self.execution.start()
        self.stopping = False
        if not self.initial_pidless_reconciled:
            await self._reconcile_processless_running(force_pidless=True)
            self.initial_pidless_reconciled = True
        for queue_id in range(1, self.queue_workers + 1):
            worker = self.workers.get(queue_id)
            if worker is None or worker.done():
                self.workers[queue_id] = asyncio.create_task(
                    self._worker(queue_id), name=f"terminal-worker-q{queue_id}"
                )
        if self.reconciler_task is None or self.reconciler_task.done():
            self.reconciler_task = asyncio.create_task(
                self._reconciler(), name="terminal-finalization-reconciler"
            )

    async def stop(self):
        self.stopping = True
        if self.reconciler_task is not None:
            self.reconciler_task.cancel()
            await asyncio.gather(self.reconciler_task, return_exceptions=True)
            self.reconciler_task = None
        processes = list(self.processes.values())
        if processes:
            await asyncio.gather(
                *(self._terminate(process, grace_seconds=self.grace) for process in processes),
                return_exceptions=True,
            )
            await asyncio.sleep(0)
        workers = list(self.workers.values())
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self.workers.clear()
        with self.claimed_commands_lock:
            self.claimed_commands.clear()
        self.pidless_first_seen.clear()
        await self.execution.close()

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

    def _emit_runtime_event(self, event, level="WARNING", **fields):
        events = getattr(self.repo, "events", None)
        if events:
            events.emit(event, level=level, **fields)

    def _update_finalization_metric(self):
        metrics = getattr(self.repo, "metrics", None)
        if metrics:
            metrics.set("terminal_mcp_finalization_pending", len(self.finalization_pending))

    @staticmethod
    def _storage_retry_delay(exc, attempt):
        name = (getattr(exc, "sqlite_errorname", None) or "").upper()
        if name.startswith("SQLITE_IOERR"):
            return min(2.0, 0.25 * (2 ** (attempt - 1)))
        if name.startswith("SQLITE_BUSY") or name.startswith("SQLITE_LOCKED"):
            return min(1.0, 0.1 * (2 ** (attempt - 1)))
        return 0.05 * (2 ** (attempt - 1))

    async def _finish_with_retry(self, command, status, exit_code=None, error=None, attempts=3):
        pending_before = command.cmd_hash in self.finalization_pending
        for attempt in range(1, attempts + 1):
            retry_exc = None
            try:
                changed = await self.repo.finish_running(command.cmd_hash, status, exit_code, error)
                if changed:
                    self.finalization_pending.pop(command.cmd_hash, None)
                    self._update_finalization_metric()
                    if pending_before or attempt > 1:
                        self._emit_runtime_event(
                            "runtime_finalization_recovered",
                            level="INFO",
                            outcome="success",
                            command_hash=command.cmd_hash,
                            queue_id=command.queue_id,
                            terminal_status=status,
                            attempt=attempt,
                        )
                    return True
                current = await self.repo.get(command.cmd_hash)
                if current is None or current.status != "running":
                    self.finalization_pending.pop(command.cmd_hash, None)
                    self._update_finalization_metric()
                    return current is not None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                retry_exc = exc
                already_pending = command.cmd_hash in self.finalization_pending
                self.finalization_pending[command.cmd_hash] = {
                    "status": status,
                    "exit_code": exit_code,
                    "error": error,
                    "queue_id": command.queue_id,
                    "attempt": attempt,
                    "exception_class": type(exc).__name__,
                }
                self._update_finalization_metric()
                if not already_pending:
                    self._emit_runtime_event(
                        "runtime_finalization_pending",
                        outcome="error",
                        command_hash=command.cmd_hash,
                        queue_id=command.queue_id,
                        terminal_status=status,
                        attempt=attempt,
                        exception_class=type(exc).__name__,
                    )
            if attempt < attempts:
                await asyncio.sleep(self._storage_retry_delay(retry_exc, attempt))
        return False

    async def finalize_running(self, command, status, exit_code=None, error=None):
        return await self._finish_with_retry(command, status, exit_code, error)

    def _mark_claimed(self, cmd_hash):
        with self.claimed_commands_lock:
            self.claimed_commands.add(cmd_hash)

    def _release_claimed(self, cmd_hash):
        with self.claimed_commands_lock:
            self.claimed_commands.discard(cmd_hash)

    def _owns_pidless_running(self, command):
        with self.claimed_commands_lock:
            return command.cmd_hash in self.claimed_commands

    async def _reconcile_processless_running(self, *, force_pidless=False):
        running = await self.repo.list_running()
        running_hashes = {command.cmd_hash for command in running}
        for command in running:
            cmd_hash = command.cmd_hash
            process = self.processes.get(cmd_hash)
            if process is not None and (await self.execution.status(process)).exit_code is None:
                continue
            if cmd_hash in self.execution_done:
                continue

            pending = self.finalization_pending.get(cmd_hash)
            if pending is not None:
                if await self._finish_with_retry(
                    command,
                    pending["status"],
                    pending["exit_code"],
                    pending["error"],
                ):
                    self.pidless_first_seen.pop(cmd_hash, None)
                continue

            if command.pid is None:
                if self._owns_pidless_running(command):
                    self.pidless_first_seen.pop(cmd_hash, None)
                    continue
                first_seen = self.pidless_first_seen.setdefault(cmd_hash, time.monotonic())
                claim_grace = max(1.0, self.queue_reconcile_sec * 2)
                if not force_pidless and time.monotonic() - first_seen < claim_grace:
                    continue
                reconcile_error = "runtime.reconcile: process was never attached"
            else:
                self.pidless_first_seen.pop(cmd_hash, None)
                if await self._pid_exists(command.pid):
                    continue
                reconcile_error = "runtime.reconcile: process no longer exists"

            if await self._finish_with_retry(
                command,
                "failed",
                command.exit_code,
                reconcile_error,
            ):
                self.pidless_first_seen.pop(cmd_hash, None)
                self._emit_runtime_event(
                    "runtime_stale_reconciled",
                    level="WARNING",
                    outcome="recovered",
                    command_hash=cmd_hash,
                    queue_id=command.queue_id,
                )

        for cmd_hash in list(self.pidless_first_seen):
            if cmd_hash not in running_hashes:
                self.pidless_first_seen.pop(cmd_hash, None)

    async def _reconciler(self):
        while not self.stopping:
            try:
                await self._reconcile_processless_running()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._emit_runtime_event(
                    "worker_failed",
                    outcome="error",
                    worker="finalization-reconciler",
                    exception_class=type(exc).__name__,
                )
            await asyncio.sleep(self.queue_reconcile_sec)

    async def _worker(self, queue_id):
        while not self.stopping:
            command = None
            try:
                command = await self.repo.claim_next(queue_id)
                if command is not None:
                    self._mark_claimed(command.cmd_hash)
                if command is None:
                    await self._wait_for_work(queue_id)
                    continue
                await self._execute(command, method="run")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._emit_runtime_event(
                    "worker_failed",
                    outcome="error",
                    queue_id=queue_id,
                    command_hash=command.cmd_hash if command else None,
                    exception_class=type(exc).__name__,
                )
                if command is not None:
                    await self._finish_with_retry(
                        command,
                        "failed",
                        command.exit_code,
                        f"run.worker: {type(exc).__name__}",
                    )
                await asyncio.sleep(min(0.1, self.queue_reconcile_sec))
            finally:
                if command is not None:
                    self._release_claimed(command.cmd_hash)

    async def _pipe_output(self, command, handle):
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
            chunk = await self.execution.read_output(handle, MAX_EXECUTION_READ_BYTES)
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

    async def _drain_output(self, command, pipe_task):
        if pipe_task is None:
            return
        if pipe_task.done():
            await pipe_task
            return
        try:
            await asyncio.wait_for(
                asyncio.shield(pipe_task),
                # Give already-produced output enough time to reach durable storage even
                # when tests/config use a very small process grace. Inherited descriptors
                # are still bounded; production grace values above this floor are unchanged.
                timeout=max(0.5, float(self.grace)),
            )
        except TimeoutError:
            await self.repo.mark_output_truncated(command.cmd_hash)
            pipe_task.cancel()
            await asyncio.gather(pipe_task, return_exceptions=True)

    async def _quiesce_after_failure(self, process):
        """Only a proven stopped/missing process releases durable queue authority."""
        if process is None:
            return True, None
        try:
            status = await self.execution.status(process)
            if status.exit_code is None:
                if not await self._terminate(process, grace_seconds=0.5):
                    return False, None
                status = await self.execution.status(process)
            return status.exit_code is not None, status.exit_code
        except Exception:
            try:
                return not await self.execution.process_exists(process.pid), None
            except Exception:
                return False, None

    async def _execute(self, command, *, method, timeout_seconds=None):
        started = time.monotonic()
        current = await self.repo.get(command.cmd_hash)
        if current is None or current.status != "running":
            return round((time.monotonic() - started) * 1000)
        command = current
        if command.cmd_hash in self.cancel_requested:
            await self._finish_with_retry(command, "cancelled")
            self.cancel_requested.discard(command.cmd_hash)
            return round((time.monotonic() - started) * 1000)

        process = None
        pipe_task = None
        error = None
        final_status = None
        execution_done = asyncio.Event()
        self.execution_done[command.cmd_hash] = execution_done
        try:
            process = await self._spawn(execution_id=command.cmd_hash)
            self.processes[command.cmd_hash] = process
            self.process_queues[command.cmd_hash] = command.queue_id
            command.pid = process.pid
            if not await self.repo.set_pid(command.cmd_hash, process.pid):
                await self._terminate(process, grace_seconds=0.1)
                return round((time.monotonic() - started) * 1000)
            await self.execution.write_stdin(process, command.cmd)
            pipe_task = asyncio.create_task(self._pipe_output(command, process))
            try:
                await self._wait_root_exit(process, timeout_seconds)
            except TimeoutError:
                error = f"{method}.timeout: command exceeded {round(timeout_seconds * 1000)} ms"
                self.cancel_requested.add(command.cmd_hash)
                await self._terminate(process, grace_seconds=0.5)
            await self._drain_output(command, pipe_task)

            command.exit_code = (await self.execution.status(process)).exit_code
            if command.cmd_hash in self.cancel_requested:
                final_status = "cancelled"
            else:
                final_status = (
                    "completed"
                    if (await self.execution.status(process)).exit_code == 0
                    else "failed"
                )
        except asyncio.CancelledError:
            self.cancel_requested.add(command.cmd_hash)
            quiesced, exit_code = await self._quiesce_after_failure(process)
            command.exit_code = exit_code
            final_status = "cancelled" if quiesced else None
            error = f"{method}.cancelled: upstream disconnected"
            raise
        except Exception as exc:
            quiesced, exit_code = await self._quiesce_after_failure(process)
            command.exit_code = exit_code
            final_status = "failed" if quiesced else None
            error = f"{method}.execute: {exc}"
            if not quiesced:
                self._emit_runtime_event(
                    "runtime_execution_uncertain",
                    level="ERROR",
                    command_hash=command.cmd_hash,
                    queue_id=command.queue_id,
                )
        finally:
            try:
                if pipe_task is not None:
                    if not pipe_task.done():
                        pipe_task.cancel()
                    await asyncio.gather(pipe_task, return_exceptions=True)
                self.processes.pop(command.cmd_hash, None)
                self.process_queues.pop(command.cmd_hash, None)
                try:
                    await self._release_output_stream(process)
                except Exception as cleanup_error:
                    self._emit_runtime_event(
                        "runtime_execution_cleanup_failed",
                        level="ERROR",
                        command_hash=command.cmd_hash,
                        error=str(cleanup_error)[:512],
                    )
                if final_status is not None:
                    finalized = await self._finish_with_retry(
                        command, final_status, command.exit_code, error
                    )
                    if finalized:
                        await self.repo.prune_output_cache()
            finally:
                self.cancel_requested.discard(command.cmd_hash)
                if command.queue_id in self.queue_events:
                    self.queue_events[command.queue_id].set()
                self.execution_done.pop(command.cmd_hash, None)
                execution_done.set()
        return round((time.monotonic() - started) * 1000)

    async def recovery(self, command, timeout_seconds=20):
        return await self._execute(
            command,
            method="recovery",
            timeout_seconds=timeout_seconds,
        )

    def _clear_finalization_pending(self, cmd_hash):
        removed = self.finalization_pending.pop(cmd_hash, None)
        if removed is not None:
            self._update_finalization_metric()
        return removed

    async def _cancel_without_owned_process(self, command):
        if command.pid is not None and await self._pid_exists(command.pid):
            return False, "cancel.unowned_process: durable process exists without local ownership"

        pending = self.finalization_pending.get(command.cmd_hash)
        pending_exit_code = pending.get("exit_code") if pending else None
        observed, repaired = await self.repo.cancel_stale_running(
            command.cmd_hash,
            command.pid,
            pending_exit_code if pending_exit_code is not None else command.exit_code,
        )
        if observed is None:
            return False, "cancel.lookup: command not found"

        if repaired or observed.status == "cancelled":
            had_pending = self._clear_finalization_pending(command.cmd_hash) is not None
            self.cancel_requested.discard(command.cmd_hash)
            if command.queue_id in self.queue_events:
                self.queue_events[command.queue_id].set()
            if repaired:
                self._emit_runtime_event(
                    "runtime_stale_cancel_repaired",
                    level="WARNING",
                    outcome="recovered",
                    command_hash=command.cmd_hash,
                    queue_id=command.queue_id,
                    had_finalization_pending=had_pending,
                    pid_was_recorded=command.pid is not None,
                )
            return True, None

        if observed.status != "running":
            return False, f"cancel.state: command is already {observed.status}"
        if observed.pid != command.pid:
            return False, "cancel.race: command ownership changed"
        return False, "cancel.stale_repair: durable state did not change"

    async def cancel(self, command, timeout_seconds=10):
        deadline = time.monotonic() + timeout_seconds
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

        process = self.processes.get(command.cmd_hash)
        if process is None:
            return await self._cancel_without_owned_process(command)

        self.cancel_requested.add(command.cmd_hash)
        if (await self.execution.status(process)).exit_code is None:
            await self.execution.signal(process, "terminate")
            try:
                term_wait = min(5.0, max(0.0, timeout_seconds - 0.5))
                await self._wait_root_exit(process, term_wait)
            except TimeoutError:
                await self.execution.signal(process, "kill")
                try:
                    kill_wait = min(3.0, max(0.0, deadline - time.monotonic()))
                    await self._wait_root_exit(process, kill_wait)
                except TimeoutError:
                    self.cancel_requested.discard(command.cmd_hash)
                    return False, (
                        "cancel.wait_process: process did not stop within "
                        f"{round(timeout_seconds * 1000)} ms"
                    )

        finalized = await self._finish_with_retry(
            command, "cancelled", (await self.execution.status(process)).exit_code, None
        )
        if not finalized:
            return False, "cancel.finalize: durable state pending"

        execution_done = self.execution_done.get(command.cmd_hash)
        if execution_done is not None:
            try:
                await asyncio.wait_for(
                    execution_done.wait(),
                    max(0.0, deadline - time.monotonic()),
                )
            except TimeoutError:
                return False, "cancel.wait_cleanup: worker cleanup did not finish before deadline"
        current = await self.repo.get(command.cmd_hash)
        if command.cmd_hash in self.processes:
            return False, "cancel.wait_cleanup: worker cleanup did not finish before deadline"
        return (current is not None and current.status == "cancelled"), None

    async def least_loaded_queue(self):
        loads = await self.repo.queue_loads(self.queue_workers)
        return min(loads, key=lambda queue_id: (loads[queue_id], queue_id))

    async def health(self):
        identity = await self.execution.health()
        queues = await self.repo.queue_snapshot(self.queue_workers)
        durable_running = await self.repo.list_running()
        live = {
            cmd_hash
            for cmd_hash, process in self.processes.items()
            if (await self.execution.status(process)).exit_code is None
        }
        pending = sorted(self.finalization_pending)
        stale = []
        unowned = []
        for command in durable_running:
            if command.cmd_hash in live or command.cmd_hash in self.execution_done:
                continue
            if command.cmd_hash in self.finalization_pending:
                continue
            if command.pid is None:
                if not self._owns_pidless_running(command):
                    stale.append(command.cmd_hash)
                continue
            if await self._pid_exists(command.pid):
                unowned.append(command.cmd_hash)
            else:
                stale.append(command.cmd_hash)

        live_by_queue = {
            queue_id: cmd_hash
            for cmd_hash, queue_id in self.process_queues.items()
            if cmd_hash in live
        }
        for item in queues:
            durable = item["running"]
            item["durable_running"] = durable
            item["running"] = live_by_queue.get(item["queue_id"])

        worker_health = {
            str(queue_id): not worker.done() for queue_id, worker in self.workers.items()
        }
        degraded = bool(pending or stale or unowned)
        output_cache = await self.repo.output_cache_stats()
        return {
            "ok": bool(self.workers) and all(worker_health.values()) and not degraded,
            **identity,
            "scheduler": "numbered-fifo",
            "parallelism": self.queue_workers,
            "queue_size": sum(item["queued"] for item in queues),
            "running_commands": sorted(live),
            "finalization_pending_commands": pending,
            "stale_running_commands": sorted(stale),
            "unowned_running_commands": sorted(unowned),
            "degraded": degraded,
            "queues": queues,
            "worker_health": worker_health,
            "output_cache": output_cache,
        }
