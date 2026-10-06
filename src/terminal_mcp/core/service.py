import asyncio
import secrets
from sqlite3 import IntegrityError

from terminal_mcp.core.agent_policy import AgentPolicy
from terminal_mcp.core.agents import AgentCoordinator
from terminal_mcp.core.orchestration import normalize_preview, public_agent_name
from terminal_mcp.core.persistent_agents import ClaimOwner
from terminal_mcp.core.tasks import VALUE_PRIORITY, TaskCoordinator
from terminal_mcp.host_resources import collect_host_resources
from terminal_mcp.storage.agents import AgentStore
from terminal_mcp.storage.context import ContextStore
from terminal_mcp.storage.events import EventJournalStore
from terminal_mcp.storage.tasks import TaskStore
from terminal_mcp.version import __version__

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


def validate_context_request(
    action,
    *,
    context_id=None,
    summary=None,
    content=None,
    primary=None,
    namespace=None,
    show_details=False,
    limit=None,
    offset=0,
):
    if action == "list":
        if any(value is not None for value in (context_id, summary, content, primary)):
            return "context.list: list accepts only read controls"
        return None
    if show_details or limit is not None or offset:
        return f"context.{action}: read controls are only valid for list"
    if action == "create":
        if context_id is not None:
            return "context.create: create does not accept id"
        if summary is None or content is None or primary is None:
            return "context.create: create requires summary, content and primary"
        return None
    if action == "update":
        if context_id is None:
            return "context.update: update requires id"
        if all(value is None for value in (summary, content, primary)):
            return "context.update: update requires at least one of summary, content or primary"
        return None
    if action == "delete":
        if context_id is None:
            return "context.delete: delete requires id"
        if any(value is not None for value in (summary, content, primary)):
            return "context.delete: delete accepts only id"
        return None
    return "context.action: action must be one of list, create, update, delete"


def _budget(seconds):
    return min(seconds, OPERATION_TIMEOUT_SECONDS)


def _error(method, stage, exc):
    reason = str(exc).strip() or exc.__class__.__name__
    return f"{method}.{stage}: {reason}"


def _compact_context(context):
    """Return only coordination data that changes the caller's next action."""
    keep = {}
    truthy = (
        "session_expired",
        "registration_required",
        "admission_required",
        "return_to_chat",
        "task_context_expired",
        "coordination_message_pending",
        "unread_message_pending",
        "reply_required_pending",
        "alert_pending",
    )
    for key in truthy:
        if context.get(key):
            keep[key] = True
    for key in ("pending_messages", "alert_messages", "reply_required_messages"):
        if context.get(key):
            keep[key] = context[key]
    if context.get("task_scope_options"):
        keep["task_scope_options"] = context["task_scope_options"]
    if context.get("session_warning"):
        keep["session_warning"] = context["session_warning"]
        keep["session_remaining_seconds"] = context.get("session_remaining_seconds")
    return keep


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
        fleet_replication=None,
        legacy_agent_admission_enabled=True,
        persistent_agents_enabled=False,
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
        self.fleet_replication = fleet_replication
        self.legacy_agent_admission_enabled = bool(legacy_agent_admission_enabled)
        self.persistent_agents_enabled = bool(persistent_agents_enabled)
        self.agent_store = AgentStore(repo.path) if hasattr(repo, "path") else None
        self.context_store = ContextStore(repo.path) if hasattr(repo, "path") else None
        self.task_store = TaskStore(repo.path) if hasattr(repo, "path") else None
        self.event_store = EventJournalStore(repo.path) if hasattr(repo, "path") else None
        self._last_health_signature = None
        self.agent_coordinator = (
            AgentCoordinator(
                self.agent_store,
                metrics,
                self.agent_policy,
                task_store=self.task_store,
                foreign_session_resolver=(
                    fleet_replication.resolve_session if fleet_replication else None
                ),
                local_instance_id=(
                    fleet_replication.config.instance_id if fleet_replication else None
                ),
                session_update_notifier=(
                    getattr(fleet_replication, "queue_session_update", None)
                    if fleet_replication
                    else None
                ),
            )
            if self.agent_store
            else None
        )
        self.task_coordinator = (
            TaskCoordinator(
                self.task_store,
                self.agent_store,
                metrics,
                session_ttl_seconds=self.agent_policy.idle_ttl_seconds,
                max_session_seconds=self.agent_policy.max_session_seconds,
            )
            if self.task_store
            else None
        )
        if self.fleet_replication:
            self.fleet_replication.bind_origin_finish_handler(self._finish_fleet_origin_session)

    async def reconcile_agent_sessions(self):
        if not self.agent_coordinator:
            return {"examined": 0, "backfilled": 0, "expired": 0}
        return await self.agent_coordinator.reconcile_sessions()

    async def _operational_context(self, agent_id, tool):
        if not agent_id or not self.agent_coordinator:
            return {}
        lifecycle = await self.agent_coordinator.touch_if_active(agent_id, tool)
        if lifecycle:
            return _compact_context(lifecycle)
        return _compact_context(await self.awareness(agent_id, surface_messages=True))

    async def _task_scope_state(self, agent_id):
        refs = []
        if agent_id and self.task_coordinator:
            refs = await self.task_coordinator.task_refs_for_agent(agent_id)
        concrete = [f"{item['namespace']}/{item['task_id']}" for item in refs]
        options = ["none"]
        if concrete:
            options.extend(["all", *concrete])
        return refs, options

    async def awareness(self, agent_id=None, *, surface_messages=False):
        active_agents = (
            await self.agent_coordinator.active_snapshot(exclude_agent_id=agent_id)
            if self.agent_coordinator
            else []
        )
        _, task_scope_options = await self._task_scope_state(agent_id)
        if not self.agent_coordinator or not agent_id:
            return {
                "agent_name": public_agent_name(agent_id),
                "active_agents": active_agents,
                "pending_messages": [],
                "alert_messages": [],
                "reply_required_messages": [],
                "task_scope_options": task_scope_options,
            }
        return {
            "active_agents": active_agents,
            "task_scope_options": task_scope_options,
            **await self.agent_coordinator.session_context(agent_id),
            **await self.agent_coordinator.message_state(agent_id, surface=surface_messages),
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
                    command_preview=normalize_preview(cmd, self.agent_policy.command_preview_chars),
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
                raise ValueError(f"queue_id must be between 1 and {self.terminal.queue_workers}")
            return requested_queue_id
        return await self.terminal.least_loaded_queue()

    async def run(self, cmd, agent_id=None, queue_id=None, task_scope=None):
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
                _, task_scope_options = await self._task_scope_state(agent_id)
                response.update(
                    {
                        "active_agents": await self.agent_coordinator.active_snapshot(
                            exclude_agent_id=agent_id
                        ),
                        "task_scope_options": task_scope_options,
                    }
                )
                return response
            gate_context = _compact_context(gate["context"])
        available_task_refs, task_scope_options = await self._task_scope_state(agent_id)
        selected_task_refs = []
        if task_scope not in task_scope_options:
            concrete = task_scope_options[2:] if len(task_scope_options) > 2 else []
            if task_scope is None:
                if concrete:
                    error = (
                        "run.task_scope: required; choose 'none', 'all', "
                        f"or one claimed task: {', '.join(concrete)}"
                    )
                else:
                    error = "run.task_scope: required; no active task claims, use 'none'"
            elif concrete:
                error = (
                    f"run.task_scope: {task_scope!r} is invalid; choose 'none', 'all', "
                    f"or one claimed task: {', '.join(concrete)}"
                )
            else:
                error = (
                    f"run.task_scope: {task_scope!r} is invalid; no active task claims, use 'none'"
                )
            return {
                "ok": False,
                "cmd_hash": None,
                "queue_id": None,
                "queue_position": None,
                "task_scope": task_scope,
                "task_targets": [],
                "task_scope_options": task_scope_options,
                "error": error,
                **gate_context,
            }
        if task_scope == "all":
            selected_task_refs = available_task_refs
        elif task_scope != "none":
            selected_task_refs = [
                item
                for item in available_task_refs
                if f"{item['namespace']}/{item['task_id']}" == task_scope
            ]
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
                await self.agent_coordinator.record_command(agent_id, "run", command.cmd_hash)
                if self.task_coordinator and selected_task_refs:
                    await self.task_coordinator.record_command(
                        agent_id, command.cmd_hash, "run", selected_task_refs
                    )
            position = await self.repo.queue_position(command.cmd_hash)
            return {
                "ok": True,
                "cmd_hash": command.cmd_hash,
                "queue_id": command.queue_id,
                "queue_position": position,
                "task_scope": task_scope,
                "task_targets": [
                    f"{item['namespace']}/{item['task_id']}" for item in selected_task_refs
                ],
                "task_scope_options": task_scope_options,
                "error": None,
                **gate_context,
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
                **(await self.awareness(agent_id) if agent_id else {}),
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
                **(await self.awareness(agent_id) if agent_id else {}),
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

    async def read(self, cmd_hash=None, lines_count=DEFAULT_READ_LINES, offset=None, agent_id=None):
        gate_context = {}
        if agent_id and self.agent_coordinator:
            gate = await self.agent_coordinator.gate(agent_id, "read", surface_messages=True)
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
                    "output_truncated": None,
                    "output_retained": None,
                    "output_pruned_at": None,
                    "output_bytes": None,
                    **gate["response"],
                }
            gate_context = _compact_context(gate["context"])
        if self.runtime:
            await self.runtime.before_tool_call()
        limit = max(1, min(int(lines_count), MAX_READ_LINES))
        result = {
            "ok": True,
            **gate_context,
            "lines": [],
            "next_offset": 0,
            "overall_lines_count": None,
            "displayed_lines_count": 0,
            "cmd_hash": cmd_hash,
            "status": None,
            "exit_code": None,
            "queue_id": None,
            "execution_started": None,
            "claimed_at": None,
            "started_at": None,
            "finished_at": None,
            "queue_position": None,
            "output_truncated": None,
            "output_retained": None,
            "output_pruned_at": None,
            "output_bytes": None,
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
                    result["execution_started"] = bool(
                        command.claimed_at is not None or command.started_at is not None
                    )
                    result["claimed_at"] = command.claimed_at
                    result["started_at"] = command.started_at
                    result["finished_at"] = command.finished_at
                    result["queue_position"] = await self.repo.queue_position(cmd_hash)
                    result.update(await self.repo.output_status(cmd_hash))
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
        context = await self._operational_context(agent_id, "recovery")
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
                await self.agent_coordinator.record_command(agent_id, "recovery", command.cmd_hash)
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
            output_status = await self.repo.output_status(command.cmd_hash)
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
                **output_status,
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
                await self.terminal.finalize_running(
                    command, "failed", command.exit_code, _error("recovery", stage, exc)
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
                "output_truncated": None,
                "output_retained": None,
                "output_pruned_at": None,
                "output_bytes": None,
                **context,
            }

    async def cancel(self, cmd_hash, agent_id=None):
        context = await self._operational_context(agent_id, "cancel")
        if self.runtime:
            await self.runtime.before_tool_call()
        stage = "arbitrate"
        execution_started = None
        try:
            async with asyncio.timeout(_budget(CANCEL_TIMEOUT_SECONDS)):
                persistent = await self.repo.persistent_attribution(cmd_hash)
                if persistent is not None:
                    return {
                        "ok": False,
                        "cmd_hash": cmd_hash,
                        "error": "cancel.persistent: exact work-session context required",
                        "cancelled_from": None,
                        "execution_started": None,
                        **context,
                    }
                command, cancelled_before_start = await self.repo.cancel_if_queued(cmd_hash)
                if command is None:
                    return {
                        "ok": False,
                        "cmd_hash": cmd_hash,
                        "error": "cancel.lookup: command not found",
                        "cancelled_from": None,
                        "execution_started": None,
                        **context,
                    }

                execution_started = bool(
                    command.claimed_at is not None or command.started_at is not None
                )
                if cancelled_before_start:
                    return {
                        "ok": True,
                        "cmd_hash": cmd_hash,
                        "error": None,
                        "cancelled_from": "queued",
                        "execution_started": False,
                        **context,
                    }

                if command.status != "running":
                    if command.status == "cancelled" and execution_started:
                        return {
                            "ok": True,
                            "cmd_hash": cmd_hash,
                            "error": None,
                            "cancelled_from": "running",
                            "execution_started": True,
                            **context,
                        }
                    return {
                        "ok": False,
                        "cmd_hash": cmd_hash,
                        "error": f"cancel.state: command is already {command.status}",
                        "cancelled_from": None,
                        "execution_started": execution_started,
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
                    "cancelled_from": "running" if ok else None,
                    "execution_started": True,
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
                "cancelled_from": None,
                "execution_started": execution_started,
                **context,
            }
        except Exception as exc:
            return {
                "ok": False,
                "cmd_hash": cmd_hash,
                "error": _error("cancel", stage, exc),
                "cancelled_from": None,
                "execution_started": execution_started,
                **context,
            }

    async def health(self, auth_mode, agent_id=None):
        context = await self._operational_context(agent_id, "health")
        if self.runtime:
            await self.runtime.before_tool_call()
        timeout = 5 if self.health_command else HEALTH_TIMEOUT_SECONDS
        try:
            async with asyncio.timeout(_budget(timeout)):
                terminal = await self.terminal.health()
                storage_ok = await self.repo.ping()
                fleet_control = getattr(self, "fleet_control", None)
                if fleet_control is not None:
                    storage_ok = bool(storage_ok and await fleet_control.healthy())
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
                    "version": __version__,
                    "storage": "ok" if storage_ok else "error",
                    "auth_mode": auth_mode,
                    "terminal": terminal,
                    **context,
                }
                if self.task_coordinator:
                    result["workflow"] = await self.task_coordinator.health()
                if self.event_store:
                    signature = {
                        "ok": bool(result["ok"]),
                        "storage": result["storage"],
                        "terminal_ok": bool(terminal.get("ok", False)),
                        "worker_health": terminal.get("worker_health") or {},
                    }
                    if signature != self._last_health_signature:
                        await self.event_store.append(
                            "health.changed",
                            "health",
                            "terminal-mcp",
                            payload=signature,
                        )
                        self._last_health_signature = signature
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
                "version": __version__,
                "storage": "error",
                "auth_mode": auth_mode,
                "terminal": terminal,
                **context,
            }

    async def _persistent_console_snapshot(self, policy: dict | None):
        if policy is None:
            return None
        projection = {
            "enabled": bool(policy.get("enabled")),
            "available": False,
            "policy": {key: value for key, value in policy.items() if key != "enabled"},
            "slots": [],
        }
        if not projection["enabled"]:
            return projection
        backend = getattr(self, "persistent", None)
        if backend is None:
            projection["error"] = "policy_incompatible"
            return projection
        result = await backend.slot_list()
        if not result.get("ok"):
            projection["error"] = (
                result.get("code") or result.get("error") or "persistent_unavailable"
            )
            return projection
        projection["available"] = True
        projection["server_now"] = result.get("server_now")
        # Slot enrichment used to run serially. A busy authority with dozens of Persistent
        # slots could therefore spend more than the Console client request deadline in
        # this read-only projection, leaving the UI stuck on cached/stale data. Bound the
        # fan-out so independent SQLite reads overlap without opening an unbounded number
        # of connections. asyncio.gather preserves the authoritative slot-list order.
        enrichment_limit = asyncio.Semaphore(8)

        async def enrich_slot(item):
            async with enrichment_limit:
                slot = item.get("slot") or {}
                logical_agent_id = slot.get("logical_agent_id")
                claims = []
                audit = []
                attachments = []
                if logical_agent_id and self.task_store:
                    claims = await self.task_store.claims_for_owner(
                        ClaimOwner.logical_agent(logical_agent_id)
                    )
                    claims = [
                        {
                            **claim,
                            "priority": VALUE_PRIORITY.get(int(claim.get("priority", 0)), "P3"),
                        }
                        for claim in claims
                    ]
                if logical_agent_id:
                    audit = await backend.lifecycle.store.audit_events(logical_agent_id, limit=50)
                work_session = item.get("work_session")
                if logical_agent_id and work_session:
                    attachments = await backend.lifecycle.store.attachments_for_session(
                        logical_agent_id,
                        work_session["work_session_id"],
                        int(work_session["session_epoch"]),
                    )
                return {**item, "claims": claims, "audit": audit, "attachments": attachments}

        projection["slots"] = list(
            await asyncio.gather(*(enrich_slot(item) for item in (result.get("slots") or [])))
        )
        return projection

    async def console_snapshot(
        self,
        auth_mode,
        *,
        public_base_url,
        history_minutes=60,
        persistent_policy=None,
    ):
        if not self.event_store or not self.agent_coordinator or not self.task_coordinator:
            return {
                "ok": False,
                "high_water_seq": 0,
                "consistency": {
                    "mode": "cursor_first_at_least_once",
                    "high_water_seq": 0,
                    "replay_from_seq": 0,
                    "duplicate_events_possible": True,
                },
                "instance": {},
                "agents": {},
                "tasks": {},
                "contexts": {},
                "communications": [],
                "persistent": await self._persistent_console_snapshot(persistent_policy),
                "error": "console snapshot unavailable",
            }

        # Cursor-first is deliberate: anything committed after this point is replayable.
        # Snapshot reads may observe newer state, so clients must apply replay idempotently.
        high_water_seq = await self.event_store.high_water_seq()

        health = await self.health(auth_mode)
        agents = await self.agent_coordinator.overview(
            agent_id=None,
            show_details=True,
            show_intents=False,
            show_commands=False,
            since_minutes=history_minutes,
            touch=False,
            reveal_agent_ids=True,
        )
        contexts = await self.context("list", show_details=True)

        task_page = await self.tasks(
            show_done=True,
            show_archived=True,
            show_details=False,
            limit=1000,
            cursor=0,
            reveal_agent_ids=True,
        )
        task_items = list(task_page.get("tasks") or [])
        next_cursor = task_page.get("next_cursor")
        while next_cursor is not None:
            page = await self.tasks(
                show_done=True,
                show_archived=True,
                show_details=False,
                limit=1000,
                cursor=next_cursor,
                reveal_agent_ids=True,
            )
            task_items.extend(page.get("tasks") or [])
            next_cursor = page.get("next_cursor")
        tasks = {
            "summary": task_page.get("summary") or {},
            "tag_counts": task_page.get("tag_counts") or {},
            "recommended": task_page.get("recommended"),
            "tasks": task_items,
        }

        communications = []
        for session in agents.get("sessions") or []:
            name = session["name"]
            stable_agent_id = session.get("agent_id")
            detail = await self.agent_coordinator.overview(
                agent_id=None,
                target=name,
                target_agent_id=stable_agent_id,
                target_session_ref=session.get("session_ref"),
                show_details=False,
                show_intents=True,
                show_commands=False,
                since_minutes=history_minutes,
                touch=False,
            )
            selected = (detail.get("sessions") or [{}])[0]
            communications.append(
                {
                    "name": name,
                    "session_ref": session.get("session_ref"),
                    "agent_id": stable_agent_id,
                    "messages_awaiting_read": selected.get("messages_awaiting_read", 0),
                    "messages_awaiting_reply": selected.get("messages_awaiting_reply", 0),
                    "alerts_pending": selected.get("alerts_pending", 0),
                    "message_journal": selected.get("message_journal") or [],
                    "intent_journal": detail.get("intent_journal") or [],
                }
            )

        instance = {
            "application": "terminal-mcp",
            "version": __version__,
            "public_base_url": public_base_url,
            "health": health,
            "resources": collect_host_resources(),
        }
        consistency = {
            "mode": "cursor_first_at_least_once",
            "high_water_seq": high_water_seq,
            "replay_from_seq": high_water_seq,
            "duplicate_events_possible": True,
        }
        persistent = await self._persistent_console_snapshot(persistent_policy)
        return {
            "ok": True,
            "high_water_seq": high_water_seq,
            "consistency": consistency,
            "instance": instance,
            "agents": agents,
            "tasks": tasks,
            "contexts": contexts,
            "communications": communications,
            "persistent": persistent,
            "error": None,
        }

    async def agent_start(
        self,
        task_summary=None,
        intent=None,
        details=None,
        work_scope=None,
        agent_id=None,
    ):
        if not self.legacy_agent_admission_enabled:
            return {
                "ok": False,
                "code": "legacy_admission_disabled",
                "legacy_admission_disabled": True,
                "persistent_agents_enabled": self.persistent_agents_enabled,
                "error": "legacy_admission_disabled",
            }
        if not self.agent_coordinator:
            return {"ok": False, "error": "agent coordination unavailable"}
        result = await self.agent_coordinator.start(
            task_summary=task_summary,
            intent=intent,
            work_scope=work_scope,
            details=details,
            agent_id=agent_id,
        )
        self_payload = result.get("self") or {}
        scope_agent_id = self_payload.get("agent_id") or agent_id
        if scope_agent_id:
            _, result["task_scope_options"] = await self._task_scope_state(scope_agent_id)
        if result.get("ok") and self.context_store:
            result["primary_context"] = [
                entry for entry in await self.context_store.list() if entry["primary"]
            ]
        if result.get("ok") and scope_agent_id and self.fleet_replication:
            self.fleet_replication.schedule_local_sync(scope_agent_id)
        return result

    async def coordinate(self, agent_id, step=None, intent=None, show_details=False):
        if not self.agent_coordinator:
            return {"ok": False, "error": "agent coordination unavailable"}
        result = await self.agent_coordinator.coordinate(
            agent_id, step=step, intent=intent, show_details=show_details
        )
        _, result["task_scope_options"] = await self._task_scope_state(agent_id)
        if result.get("ok") and self.fleet_replication:
            self.fleet_replication.schedule_local_sync(agent_id)
        return result

    async def message(
        self,
        agent_id,
        text=None,
        target=None,
        message_hash=None,
        require_reply=False,
        alert=False,
        namespace=None,
        task_id=None,
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
            namespace=namespace,
            task_id=task_id,
        )
        result.update(
            _compact_context(
                {
                    **await self.agent_coordinator.session_context(agent_id),
                    **await self.agent_coordinator.message_state(agent_id, surface=True),
                }
            )
        )
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
        result = await self.agent_coordinator.overview(
            agent_id=agent_id,
            target=target,
            show_details=show_details,
            show_intents=show_intents,
            show_commands=show_commands,
            command_hash=command_hash,
            since_minutes=since_minutes,
        )
        if agent_id:
            _, result["task_scope_options"] = await self._task_scope_state(agent_id)
        return result

    async def context(
        self,
        action,
        *,
        context_id=None,
        summary=None,
        content=None,
        primary=None,
        namespace=None,
        show_details=False,
        limit=None,
        offset=0,
    ):
        if not self.context_store:
            return {"ok": False, "error": "instance context unavailable"}
        validation_error = validate_context_request(
            action,
            context_id=context_id,
            summary=summary,
            content=content,
            primary=primary,
            namespace=namespace,
            show_details=show_details,
            limit=limit,
            offset=offset,
        )
        if validation_error:
            return {"ok": False, "error": validation_error}
        try:
            if action == "list":
                entries = await self.context_store.list(
                    namespace=namespace,
                    limit=limit,
                    offset=offset,
                    primary_first=limit is not None,
                )

                def compact(entry):
                    item = {"id": entry["id"], "summary": entry["summary"]}
                    if entry.get("namespace") is not None:
                        item["namespace"] = entry["namespace"]
                    if show_details:
                        item["content"] = entry["content"]
                    return item

                return {
                    "ok": True,
                    "primary": [compact(item) for item in entries if item["primary"]],
                    "additional": [compact(item) for item in entries if not item["primary"]],
                }
            if action == "create":
                entry = await self.context_store.create(
                    summary, content, primary, namespace=namespace
                )
                return {"ok": True, "entry": entry}
            if action == "update":
                fields = {}
                if summary is not None:
                    fields["summary"] = summary
                if content is not None:
                    fields["content"] = content
                if primary is not None:
                    fields["primary"] = primary
                entry = await self.context_store.update(context_id, namespace=namespace, **fields)
                if entry is None:
                    return {"ok": False, "error": f"context id {context_id} not found"}
                return {"ok": True, "entry": entry}
            deleted = await self.context_store.delete(context_id, namespace=namespace)
            if not deleted:
                return {"ok": False, "error": f"context id {context_id} not found"}
            return {"ok": True, "deleted_id": int(context_id)}
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": f"context.{action}: {exc}"}

    async def tasks(
        self,
        *,
        namespace=None,
        task_id=None,
        lane=None,
        state=None,
        operational_status=None,
        tags=None,
        show_details=False,
        snapshot=False,
        show_done=False,
        show_archived=False,
        limit=50,
        cursor=None,
        reveal_agent_ids=False,
    ):
        if not self.task_coordinator:
            return {"ok": False, "error": "task coordination unavailable"}
        return await self.task_coordinator.list(
            namespace=namespace,
            task_id=task_id,
            lane=lane,
            state=state,
            operational_status=operational_status,
            tags=tags,
            show_details=show_details,
            snapshot=snapshot,
            show_done=show_done,
            show_archived=show_archived,
            limit=limit,
            cursor=cursor,
            reveal_agent_ids=reveal_agent_ids,
        )

    async def task(self, agent_id, **kwargs):
        if not self.task_coordinator or not self.agent_coordinator:
            return {"ok": False, "error": "task coordination unavailable", "warnings": []}
        gate = await self.agent_coordinator.gate(agent_id, "task", surface_messages=True)
        if gate["blocked"]:
            response = gate["response"]
            response.setdefault("warnings", [])
            response.setdefault("task", None)
            return response
        result = await self.task_coordinator.mutate(agent_id, **kwargs)
        result.update(_compact_context(gate["context"]))
        return result

    async def _finish_fleet_origin_session(self, agent_id, ended_at, reason):
        if not self.agent_store:
            return False
        session = await self.agent_store.get_session(agent_id)
        if session is None:
            return False
        if self.fleet_replication:
            source = session.get("source_instance_id")
            if source and source != self.fleet_replication.config.instance_id:
                return False
        if session["state"] == "active":
            changed = await self.agent_store.end(
                agent_id,
                "finished",
                reason,
                ended_at,
            )
            if changed and self.task_coordinator:
                await self.task_coordinator.release_agent_claims(
                    agent_id, reason="fleet_agent_finish"
                )
            return bool(changed)
        return session["state"] in {"finished", "forced"}

    async def agent_finish(self, agent_id):
        if not self.agent_coordinator:
            return {"ok": False, "error": "agent coordination unavailable"}
        pending = await self.agent_coordinator.message_state(agent_id, surface=True)
        pending_communication = await self.agent_coordinator.message_obligation_summary(agent_id)
        result = await self.agent_coordinator.finish(agent_id)
        if self.task_coordinator and result.get("finished"):
            await self.task_coordinator.release_agent_claims(agent_id, reason="agent_finish")
        if result.get("finished") and self.fleet_replication:
            queued_foreign = await self.fleet_replication.queue_finish(agent_id)
            if not queued_foreign:
                self.fleet_replication.schedule_local_sync(agent_id)
        result.update(_compact_context(pending))
        if any(pending_communication.values()):
            result["pending_communication"] = pending_communication
        return result
