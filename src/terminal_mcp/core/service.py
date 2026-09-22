import asyncio
import secrets
from sqlite3 import IntegrityError

from terminal_mcp.core.agent_policy import AgentPolicy
from terminal_mcp.core.agents import AgentCoordinator
from terminal_mcp.core.orchestration import normalize_preview, public_agent_name
from terminal_mcp.storage.agents import AgentStore

DEFAULT_READ_LINES = 500
MAX_READ_LINES = 1000
OPERATION_TIMEOUT_SECONDS = 45
RUN_TIMEOUT_SECONDS = 2
READ_TIMEOUT_SECONDS = 5
CANCEL_TIMEOUT_SECONDS = 10
RECOVERY_TIMEOUT_SECONDS = 20
HEALTH_TIMEOUT_SECONDS = 3
RECOVERY_OUTPUT_LINES = 500
ANONYMOUS_AGENT_ID = "anonymous"


def _budget(seconds):
    return min(seconds, OPERATION_TIMEOUT_SECONDS)


def _error(method, stage, exc):
    reason = str(exc).strip() or exc.__class__.__name__
    return f"{method}.{stage}: {reason}"


class TerminalService:
    def __init__(
        self,
        repo,
        terminal,
        max_lines,
        auth_mode="none",
        health_command="",
        runtime=None,
        events=None,
        metrics=None,
        agent_policy: AgentPolicy | None = None,
    ):
        self.repo = repo
        self.terminal = terminal
        self.max_lines = max_lines
        self.auth_mode = auth_mode
        self.health_command = health_command
        self.runtime = runtime
        self.events = events
        self.metrics = metrics
        self.agent_policy = agent_policy or AgentPolicy()
        self.agent_coordinator = (
            AgentCoordinator(AgentStore(repo.path), metrics, self.agent_policy)
            if hasattr(repo, "path")
            else None
        )

    async def awareness(self, agent_id=None, *, surface_messages=False):
        active_agents = (
            await self.agent_coordinator.active_snapshot(exclude_agent_id=agent_id)
            if self.agent_coordinator
            else []
        )
        if not self.agent_coordinator or not agent_id:
            return {
                "agent_name": public_agent_name(agent_id),
                "active_agents": active_agents,
                "pending_messages": [],
                "alert_messages": [],
                "reply_required_messages": [],
            }
        return {
            "active_agents": active_agents,
            **await self.agent_coordinator.session_context(agent_id),
            **await self.agent_coordinator.message_state(
                agent_id, surface=surface_messages
            ),
        }

    async def _create_with_hash(
        self, cmd, status, agent_id=None, command_type="run", queue_id=None
    ):
        attribution_agent_id = agent_id or ANONYMOUS_AGENT_ID
        for _ in range(32):
            cmd_hash = secrets.token_hex(4)
            try:
                return await self.repo.create(
                    cmd,
                    status=status,
                    cmd_hash=cmd_hash,
                    agent_id=attribution_agent_id,
                    command_type=command_type,
                    command_preview=normalize_preview(
                        cmd, self.agent_policy.command_preview_chars
                    ),
                    queue_id=queue_id,
                )
            except IntegrityError:
                continue
        raise RuntimeError("unable to allocate unique command hash")

    async def _resolve_queue(self, agent_id, requested_queue_id):
        if agent_id and self.agent_coordinator:
            return await self.agent_coordinator.resolve_queue(
                agent_id,
                requested_queue_id,
                self.terminal.queue_workers,
                self.terminal.least_loaded_queue,
            )
        if requested_queue_id is not None:
            if requested_queue_id < 1 or requested_queue_id > self.terminal.queue_workers:
                raise ValueError(
                    f"queue_id must be between 1 and {self.terminal.queue_workers}"
                )
            return requested_queue_id
        return await self.terminal.least_loaded_queue()

    async def run(self, cmd, agent_id=None, queue_id=None):
        gate_context = {}
        if agent_id and self.agent_coordinator:
            gate = await self.agent_coordinator.gate(
                agent_id, "run", require_intent=True, surface_messages=True
            )
            if gate["blocked"]:
                response = gate["response"]
                response.setdefault("cmd_hash", None)
                response.setdefault("queue_id", None)
                response.setdefault("queue_position", None)
                response.update(
                    {
                        "active_agents": await self.agent_coordinator.active_snapshot(
                            exclude_agent_id=agent_id
                        )
                    }
                )
                return response
            gate_context = gate["context"]
        if self.runtime:
            await self.runtime.before_tool_call()
        command = None
        submitted = False
        stage = "select_queue"
        try:
            async with asyncio.timeout(_budget(RUN_TIMEOUT_SECONDS)):
                selected_queue = await self._resolve_queue(agent_id, queue_id)
                stage = "persist"
                command = await self._create_with_hash(
                    cmd, "queued", agent_id, "run", queue_id=selected_queue
                )
                stage = "enqueue"
                await self.terminal.submit(command)
                submitted = True
            if agent_id and self.agent_coordinator:
                await self.agent_coordinator.record_command(
                    agent_id, "run", command.cmd_hash
                )
            position = await self.repo.queue_position(command.cmd_hash)
            return {
                "ok": True,
                "cmd_hash": command.cmd_hash,
                "queue_id": command.queue_id,
                "queue_position": position,
                "error": None,
                **gate_context,
                **(
                    {
                        "active_agents": await self.agent_coordinator.active_snapshot(
                            exclude_agent_id=agent_id
                        )
                    }
                    if agent_id and self.agent_coordinator
                    else {}
                ),
            }
        except asyncio.CancelledError:
            if command is not None and not submitted:
                if await self.repo.cancel_queued(command.cmd_hash):
                    await self.repo.delete_command(command.cmd_hash)
            if self.events:
                self.events.emit(
                    "client_disconnected",
                    transport="unknown",
                    tool="run",
                    outcome="cancelled",
                    cmd_hash=command.cmd_hash if command else None,
                )
            raise
        except TimeoutError:
            if command is not None:
                if await self.repo.cancel_queued(command.cmd_hash):
                    await self.repo.delete_command(command.cmd_hash)
            return {
                "ok": False,
                "cmd_hash": None,
                "queue_id": None,
                "queue_position": None,
                "error": f"run.{stage}: timed out after 2000 ms",
                **(
                    await self.awareness(agent_id)
                    if agent_id
                    else {}
                ),
            }
        except Exception as exc:
            if command is not None:
                if await self.repo.cancel_queued(command.cmd_hash):
                    await self.repo.delete_command(command.cmd_hash)
            return {
                "ok": False,
                "cmd_hash": None,
                "queue_id": None,
                "queue_position": None,
                "error": _error("run", stage, exc),
                **(
                    await self.awareness(agent_id)
                    if agent_id
                    else {}
                ),
            }

    @staticmethod
    def _render(lines, scoped, agent_map=None, queue_map=None):
        agent_map = agent_map or {}
        queue_map = queue_map or {}
        rendered = []
        for line in lines:
            if scoped:
                rendered.append(f"{line.appeared_at.removesuffix(chr(90))} {line.text}")
                continue
            queue = queue_map.get(line.cmd_hash)
            queue_label = f"q{queue} " if queue else ""
            rendered.append(
                f"{line.appeared_at.removesuffix(chr(90))} "
                f"{public_agent_name(agent_map.get(line.cmd_hash, ANONYMOUS_AGENT_ID))} "
                f"{line.cmd_hash} {queue_label}{line.text}"
            )
        return rendered

    async def read(
        self, cmd_hash=None, lines_count=DEFAULT_READ_LINES, offset=None, agent_id=None
    ):
        gate_context = {}
        if agent_id and self.agent_coordinator:
            gate = await self.agent_coordinator.gate(
                agent_id, "read", surface_messages=True
            )
            if gate["blocked"]:
                return {
                    "lines": [],
                    "next_offset": 0,
                    "overall_lines_count": None,
                    "displayed_lines_count": 0,
                    "cmd_hash": cmd_hash,
                    "status": None,
                    "exit_code": None,
                    "queue_id": None,
                    "queue_position": None,
                    **gate["response"],
                }
            gate_context = gate["context"]
        if self.runtime:
            await self.runtime.before_tool_call()
        limit = max(1, min(int(lines_count), MAX_READ_LINES))
        result = {
            "ok": True,
            "agent_name": public_agent_name(agent_id),
            "active_agents": (
                await self.agent_coordinator.active_snapshot(exclude_agent_id=agent_id)
                if self.agent_coordinator
                else []
            ),
            **gate_context,
            "lines": [],
            "next_offset": 0,
            "overall_lines_count": None,
            "displayed_lines_count": 0,
            "cmd_hash": cmd_hash,
            "status": None,
            "exit_code": None,
            "queue_id": None,
            "queue_position": None,
            "error": None,
        }
        stage = "load_command" if cmd_hash else "load_lines"
        try:
            async with asyncio.timeout(_budget(READ_TIMEOUT_SECONDS)):
                if cmd_hash:
                    command = await self.repo.get(cmd_hash)
                    if command is None:
                        result["status"] = "not_found"
                        result["overall_lines_count"] = 0
                        return result
                    result["status"] = command.status
                    result["exit_code"] = command.exit_code
                    result["error"] = command.error
                    result["ok"] = command.error is None
                    result["queue_id"] = command.queue_id
                    result["queue_position"] = await self.repo.queue_position(cmd_hash)
                    stage = "count_lines"
                    total = await self.repo.count_lines(cmd_hash)
                    result["overall_lines_count"] = total
                    if offset is None:
                        start = max(total - limit, 0)
                    elif offset < 0:
                        start = max(total + offset, 0)
                    else:
                        start = min(offset, total)
                    stage = "load_lines"
                    lines = await self.repo.read_command_lines(cmd_hash, limit, start)
                    result["next_offset"] = start + len(lines)
                    result["lines"] = self._render(lines, scoped=True)
                else:
                    if offset is None:
                        lines = await self.repo.read_global_tail(limit)
                        result["next_offset"] = lines[-1].seq if lines else 0
                    elif offset < 0:
                        lines = await self.repo.read_global_tail(limit, abs(offset))
                        result["next_offset"] = lines[-1].seq if lines else 0
                    else:
                        lines = await self.repo.read_global_after_cursor(limit, offset)
                        result["next_offset"] = lines[-1].seq if lines else offset
                    agent_map = {}
                    hashes = [line.cmd_hash for line in lines]
                    if self.agent_coordinator:
                        agent_map = await self.agent_coordinator.store.command_agents(hashes)
                    queue_map = await self.repo.command_queue_ids(hashes)
                    result["lines"] = self._render(
                        lines, scoped=False, agent_map=agent_map, queue_map=queue_map
                    )
                result["displayed_lines_count"] = len(result["lines"])
                return result
        except asyncio.CancelledError:
            if self.events:
                self.events.emit(
                    "client_disconnected",
                    transport="unknown",
                    tool="read",
                    outcome="cancelled",
                    cmd_hash=cmd_hash,
                )
            raise
        except TimeoutError:
            result["ok"] = False
            result["error"] = f"read.{stage}: timed out after 5000 ms"
            return result
        except Exception as exc:
            result["ok"] = False
            result["error"] = _error("read", stage, exc)
            return result

    async def recovery(self, cmd, agent_id=None):
        caller_agent_id = agent_id or ANONYMOUS_AGENT_ID
        context = {}
        if agent_id and self.agent_coordinator:
            await self.agent_coordinator.touch_if_active(agent_id, "recovery")
            context = await self.awareness(agent_id, surface_messages=True)
        if self.runtime:
            await self.runtime.before_tool_call()
        command = None
        stage = "persist"
        started_at = asyncio.get_running_loop().time()
        try:
            command = await self._create_with_hash(
                cmd, "running", caller_agent_id, "recovery", queue_id=None
            )
            if agent_id and self.agent_coordinator:
                await self.agent_coordinator.record_command(
                    agent_id, "recovery", command.cmd_hash
                )
            stage = "execute"
            duration_ms = await self.terminal.recovery(
                command, timeout_seconds=_budget(RECOVERY_TIMEOUT_SECONDS)
            )
            current = await self.repo.get(command.cmd_hash) or command
            stage = "count_lines"
            total = await self.repo.count_lines(command.cmd_hash)
            stage = "load_lines"
            start = max(total - RECOVERY_OUTPUT_LINES, 0)
            lines = await self.repo.read_command_lines(
                command.cmd_hash, RECOVERY_OUTPUT_LINES, start
            )
            plugin_error = current.error
            return {
                "ok": plugin_error is None and current.status in {"completed", "failed"},
                "agent_name": public_agent_name(caller_agent_id),
                "cmd_hash": command.cmd_hash,
                "lines": self._render(lines, scoped=True),
                "overall_lines_count": total,
                "displayed_lines_count": len(lines),
                "exit_code": current.exit_code,
                "error": plugin_error,
                "duration_ms": duration_ms,
                **context,
            }
        except asyncio.CancelledError:
            if self.events:
                self.events.emit(
                    "client_disconnected",
                    transport="unknown",
                    tool="recovery",
                    outcome="cancelled",
                    cmd_hash=command.cmd_hash if command else None,
                )
            raise
        except Exception as exc:
            elapsed = round((asyncio.get_running_loop().time() - started_at) * 1000)
            if command is not None:
                await self.repo.finish_running(
                    command.cmd_hash, "failed", command.exit_code, _error("recovery", stage, exc)
                )
            return {
                "ok": False,
                "agent_name": public_agent_name(caller_agent_id),
                "cmd_hash": command.cmd_hash if command else None,
                "lines": [],
                "overall_lines_count": 0,
                "displayed_lines_count": 0,
                "exit_code": None,
                "error": _error("recovery", stage, exc),
                "duration_ms": elapsed,
                **context,
            }

    async def cancel(self, cmd_hash, agent_id=None):
        context = {}
        if agent_id and self.agent_coordinator:
            await self.agent_coordinator.touch_if_active(agent_id, "cancel")
            context = await self.awareness(agent_id, surface_messages=True)
        if self.runtime:
            await self.runtime.before_tool_call()
        stage = "lookup"
        try:
            async with asyncio.timeout(_budget(CANCEL_TIMEOUT_SECONDS)):
                command = await self.repo.get(cmd_hash)
                if command is None:
                    return {
                        "ok": False,
                        "cmd_hash": cmd_hash,
                        "error": "cancel.lookup: command not found",
                        **context,
                    }
                stage = "stop"
                ok, error = await self.terminal.cancel(
                    command, timeout_seconds=_budget(CANCEL_TIMEOUT_SECONDS)
                )
                return {
                    "ok": ok,
                    "cmd_hash": cmd_hash,
                    "error": error,
                    **context,
                }
        except asyncio.CancelledError:
            if self.events:
                self.events.emit(
                    "client_disconnected",
                    transport="unknown",
                    tool="cancel",
                    outcome="cancelled",
                    cmd_hash=cmd_hash,
                )
            raise
        except TimeoutError:
            return {
                "ok": False,
                "cmd_hash": cmd_hash,
                "error": f"cancel.{stage}: timed out after 10000 ms",
                **context,
            }
        except Exception as exc:
            return {
                "ok": False,
                "cmd_hash": cmd_hash,
                "error": _error("cancel", stage, exc),
                **context,
            }

    async def health(self, auth_mode, agent_id=None):
        context = {}
        if agent_id and self.agent_coordinator:
            await self.agent_coordinator.touch_if_active(agent_id, "health")
            context = await self.awareness(agent_id, surface_messages=True)
        if self.runtime:
            await self.runtime.before_tool_call()
        timeout = 5 if self.health_command else HEALTH_TIMEOUT_SECONDS
        try:
            async with asyncio.timeout(_budget(timeout)):
                terminal = await self.terminal.health()
                storage_ok = await self.repo.ping()
                internal_ok = bool(
                    storage_ok
                    and (self.runtime is None or self.runtime.alive)
                    and (self.events is None or self.events.alive)
                    and (
                        self.metrics is None
                        or not self.runtime.current.metrics_enabled
                        or self.metrics.alive
                    )
                )
                result = {
                    "ok": terminal.get("ok", False) and internal_ok,
                    "agent_name": public_agent_name(agent_id),
                    "application": "terminal-mcp",
                    "storage": "ok" if storage_ok else "error",
                    "auth_mode": auth_mode,
                    "terminal": terminal,
                    **context,
                }
                if self.health_command:
                    custom = await self.terminal.capture(
                        self.health_command,
                        timeout_ms=5000,
                        max_output_lines=min(1000, self.max_lines),
                    )
                    result["custom_command"] = {
                        "command": self.health_command,
                        **custom,
                    }
                    result["ok"] = result["ok"] and custom["ok"]
                return result
        except asyncio.CancelledError:
            if self.events:
                self.events.emit(
                    "client_disconnected",
                    transport="unknown",
                    tool="health",
                    outcome="cancelled",
                )
            raise
        except TimeoutError:
            terminal = await self.terminal.health()
            terminal["ok"] = False
            return {
                "ok": False,
                "agent_name": public_agent_name(agent_id),
                "application": "terminal-mcp",
                "storage": "error",
                "auth_mode": auth_mode,
                "terminal": terminal,
                **context,
            }

    async def agent_start(
        self,
        task_summary=None,
        intent=None,
        details=None,
        work_scope=None,
        agent_id=None,
    ):
        if not self.agent_coordinator:
            return {"ok": False, "error": "agent coordination unavailable"}
        return await self.agent_coordinator.start(
            task_summary=task_summary,
            intent=intent,
            work_scope=work_scope,
            details=details,
            agent_id=agent_id,
        )

    async def coordinate(self, agent_id, step=None, intent=None, show_details=False):
        if not self.agent_coordinator:
            return {"ok": False, "error": "agent coordination unavailable"}
        return await self.agent_coordinator.coordinate(
            agent_id, step=step, intent=intent, show_details=show_details
        )

    async def message(
        self,
        agent_id,
        text=None,
        target=None,
        message_hash=None,
        require_reply=False,
        alert=False,
    ):
        if not self.agent_coordinator:
            return {"ok": False, "error": "agent coordination unavailable"}
        result = await self.agent_coordinator.message(
            agent_id,
            text=text,
            target=target,
            message_hash=message_hash,
            require_reply=require_reply,
            alert=alert,
        )
        result.update(await self.agent_coordinator.session_context(agent_id))
        result.update(await self.agent_coordinator.message_state(agent_id, surface=True))
        return result

    async def agents(
        self,
        agent_id=None,
        *,
        target=None,
        show_details=False,
        show_intents=False,
        show_commands=False,
        command_hash=None,
        since_minutes=None,
    ):
        if not self.agent_coordinator:
            return {"ok": False, "error": "agent coordination unavailable"}
        return await self.agent_coordinator.overview(
            agent_id=agent_id,
            target=target,
            show_details=show_details,
            show_intents=show_intents,
            show_commands=show_commands,
            command_hash=command_hash,
            since_minutes=since_minutes,
        )

    async def agent_finish(self, agent_id):
        if not self.agent_coordinator:
            return {"ok": False, "error": "agent coordination unavailable"}
        pending = await self.agent_coordinator.message_state(agent_id, surface=True)
        result = await self.agent_coordinator.finish(agent_id)
        result.update(pending)
        return result
