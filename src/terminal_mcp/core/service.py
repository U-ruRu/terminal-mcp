import asyncio
import secrets
from sqlite3 import Error as SqliteError
from sqlite3 import IntegrityError

from terminal_mcp.core.agent_history import AgentHistory
from terminal_mcp.core.agent_policy import AgentPolicy
from terminal_mcp.core.agent_views import AgentViews
from terminal_mcp.core.orchestration import normalize_preview, public_agent_name, utc_text
from terminal_mcp.core.tasks import TaskCoordinator
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
        self.agent_store = AgentStore(repo.path) if hasattr(repo, "path") else None
        self.context_store = ContextStore(repo.path) if hasattr(repo, "path") else None
        self.task_store = TaskStore(repo.path) if hasattr(repo, "path") else None
        self.event_store = EventJournalStore(repo.path) if hasattr(repo, "path") else None
        self._last_health_signature = None
        self.agent_history = (
            AgentHistory(self.agent_store, self.agent_policy, task_store=self.task_store)
            if self.agent_store
            else None
        )
        self.agent_views = AgentViews(self)
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
            await self.agent_views.active_snapshot(exclude_agent_id=agent_id)
            if self.agent_history
            else []
        )
        _, task_scope_options = await self._task_scope_state(agent_id)
        return {
            "agent_name": public_agent_name(agent_id),
            "active_agents": active_agents,
            "pending_messages": [],
            "alert_messages": [],
            "reply_required_messages": [],
            "task_scope_options": task_scope_options,
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
        if requested_queue_id is not None:
            if requested_queue_id < 1 or requested_queue_id > self.terminal.queue_workers:
                raise ValueError(f"queue_id must be between 1 and {self.terminal.queue_workers}")
            return requested_queue_id
        return await self.terminal.least_loaded_queue()

    async def run(self, cmd, agent_id=None, queue_id=None, task_scope=None):
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
            if agent_id and self.agent_history:
                await self.agent_store.activity(agent_id, "run", utc_text(), command.cmd_hash)
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
        if self.runtime:
            await self.runtime.before_tool_call()
        limit = max(1, min(int(lines_count), MAX_READ_LINES))
        result = {
            "ok": True,
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
                    result["cmd"] = command.cmd
                    result["status"] = command.status
                    result["exit_code"] = command.exit_code
                    result["error"] = command.error
                    # A successful read is independent from the command outcome.
                    # Failed/cancelled commands remain readable terminal records;
                    # their execution result is carried by status/exit_code/error.
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
                    if self.agent_history:
                        agent_map = await self.agent_history.store.command_agents(hashes)
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
        if self.runtime:
            await self.runtime.before_tool_call()
        command = None
        stage = "persist"
        started_at = asyncio.get_running_loop().time()
        try:
            command = await self._create_with_hash(
                cmd, "running", caller_agent_id, "recovery", queue_id=None
            )
            if agent_id and self.agent_history:
                await self.agent_store.activity(agent_id, "recovery", utc_text(), command.cmd_hash)
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
        context = {}
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
        context = {}
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
                    # Do not query another table in an already-corrupt main DB.
                    # A stale successful cache can also race with later damage.
                    result["workflow"] = {"ok": False}
                    if storage_ok:
                        try:
                            result["workflow"] = await self.task_coordinator.health()
                        except SqliteError:
                            storage_ok = False
                            result["storage"] = "error"
                    result["ok"] = bool(result["ok"] and result["workflow"].get("ok", False))
                components = [
                    {
                        "id": "storage",
                        "status": "healthy" if storage_ok else "failed",
                        "reason": None if storage_ok else "storage_unavailable",
                    },
                    {
                        "id": "terminal",
                        "status": (
                            "healthy"
                            if terminal.get("ok", False)
                            else "degraded"
                            if terminal.get("degraded")
                            else "failed"
                        ),
                        "reason": None if terminal.get("ok", False) else "terminal_unavailable",
                    },
                ]
                if self.task_coordinator:
                    workflow_ok = bool(result["workflow"].get("ok", False))
                    components.append(
                        {
                            "id": "workflow",
                            "status": "healthy" if workflow_ok else "degraded",
                            "reason": None if workflow_ok else "workflow_degraded",
                        }
                    )
                result["components"] = components
                result["status"] = (
                    "healthy"
                    if result["ok"]
                    else "failed"
                    if any(item["status"] == "failed" for item in components)
                    else "degraded"
                )
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
                    result["components"].append(
                        {
                            "id": "custom_command",
                            "status": "healthy" if custom["ok"] else "degraded",
                            "reason": None if custom["ok"] else "custom_health_failed",
                        }
                    )
                    if not custom["ok"] and result["status"] == "healthy":
                        result["status"] = "degraded"
                mesh = getattr(self, "access_mesh", None)
                if mesh is not None:
                    mesh_components = await mesh.health_components()
                    messaging = getattr(self, "access_mesh_messages", None)
                    if messaging is not None:
                        running = messaging._task is not None and not messaging._task.done()
                        failure = None
                        try:
                            probe_ok = await asyncio.to_thread(messaging.store.health_probe)
                            if not probe_ok:
                                failure = "local_message_storage_probe_failed"
                        except Exception:
                            failure = "local_message_storage_probe_failed"
                        state = (
                            "failed"
                            if failure or not running
                            else "degraded"
                            if messaging.last_error
                            else "healthy"
                        )
                        mesh_components["access_mesh_messages"] = {
                            "status": state,
                            "ok": state == "healthy",
                            "reason": failure
                            or (
                                "local_message_cleanup_not_running"
                                if not running
                                else "local_message_cleanup_failed"
                                if messaging.last_error
                                else None
                            ),
                        }
                    replication = getattr(self, "access_mesh_replication", None)
                    if replication is not None:
                        for peer, state in replication.peer_health.items():
                            mesh_components[f"access_mesh_peer:{peer}"] = {
                                **state,
                                "ok": state["status"] == "healthy",
                            }
                    components.extend(
                        {"id": name, "status": item["status"], "reason": item.get("reason")}
                        for name, item in mesh_components.items()
                    )
                    severity = {"healthy": 0, "degraded": 1, "failed": 2}
                    result["status"] = max(
                        [result["status"], *(c["status"] for c in components)],
                        key=severity.__getitem__,
                    )
                    result["ok"] = result["status"] == "healthy"
                # On failed durable storage, even recording health.changed
                # may fail on the same corrupted B-tree. Keep health observable.
                if self.event_store and storage_ok:
                    signature = {
                        "ok": bool(result["ok"]),
                        "status": result["status"],
                        "components": components,
                        "storage": result["storage"],
                        "terminal_ok": bool(terminal.get("ok", False)),
                        "worker_health": terminal.get("worker_health") or {},
                    }
                    if signature != self._last_health_signature:
                        try:
                            await self.event_store.append(
                                "health.changed",
                                "health",
                                "terminal-mcp",
                                payload=signature,
                            )
                        except SqliteError:
                            # The DB can become unreadable after a cached healthy
                            # probe; a failed diagnostic write must fail closed.
                            result["ok"] = False
                            result["storage"] = "error"
                            result["status"] = "failed"
                            components[0]["status"] = "failed"
                            components[0]["reason"] = "storage_unavailable"
                        else:
                            self._last_health_signature = signature
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
            result = {
                "ok": False,
                "agent_name": public_agent_name(agent_id),
                "application": "terminal-mcp",
                "version": __version__,
                "storage": "error",
                "auth_mode": auth_mode,
                "terminal": terminal,
                "status": "failed",
                "components": [
                    {"id": "storage", "status": "failed", "reason": "storage_unavailable"},
                    {"id": "terminal", "status": "failed", "reason": "terminal_unavailable"},
                ],
                **context,
            }
            if self.task_coordinator:
                result["workflow"] = {"ok": False}
                result["components"].append(
                    {"id": "workflow", "status": "degraded", "reason": "workflow_degraded"}
                )
            return result

    async def console_snapshot(
        self,
        auth_mode,
        *,
        public_base_url,
        history_minutes=60,
        persistent_policy=None,
    ):
        if not self.event_store or not self.agent_history or not self.task_coordinator:
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
                "error": "console snapshot unavailable",
            }

        # Cursor-first is deliberate: anything committed after this point is replayable.
        # Snapshot reads may observe newer state, so clients must apply replay idempotently.
        high_water_seq = await self.event_store.high_water_seq()

        # Independent read projections share the mobile initial-snapshot deadline.
        async with asyncio.TaskGroup():
            # Independent read-only projections share the mobile initial-snapshot deadline.
            health, agents, contexts = await asyncio.gather(
                self.health(auth_mode),
                self.agent_views.overview(
                    agent_id=None,
                    show_details=True,
                    show_intents=False,
                    show_commands=False,
                    since_minutes=history_minutes,
                    touch=False,
                    reveal_agent_ids=True,
                ),
                self.context("list", show_details=True),
            )

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

            # Bound parallel per-session reads; an active Mesh can contain dozens
            # of sessions, and serial journal lookups exhaust the Android timeout.
            communication_limit = asyncio.Semaphore(8)

            async def read_communication(session):
                async with communication_limit:
                    detail = await self.agent_views.overview(
                        agent_id=None,
                        target=session["name"],
                        target_agent_id=session.get("agent_id"),
                        target_session_ref=session.get("session_ref"),
                        show_details=False,
                        show_intents=True,
                        show_commands=False,
                        since_minutes=history_minutes,
                        touch=False,
                    )
                selected = (detail.get("sessions") or [{}])[0]
                return {
                    "name": session["name"],
                    "session_ref": session.get("session_ref"),
                    "agent_id": session.get("agent_id"),
                    "messages_awaiting_read": selected.get("messages_awaiting_read", 0),
                    "messages_awaiting_reply": selected.get("messages_awaiting_reply", 0),
                    "alerts_pending": selected.get("alerts_pending", 0),
                    "message_journal": selected.get("message_journal") or [],
                    "intent_journal": detail.get("intent_journal") or [],
                }

            communications = list(
                await asyncio.gather(
                    *(read_communication(session) for session in (agents.get("sessions") or []))
                )
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
        return {
            "ok": True,
            "high_water_seq": high_water_seq,
            "consistency": consistency,
            "instance": instance,
            "agents": agents,
            "tasks": tasks,
            "contexts": contexts,
            "communications": communications,
            "error": None,
        }

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
        history=False,
        target_session_ref=None,
    ):
        if not self.agent_history:
            return {"ok": False, "error": "agent coordination unavailable"}
        result = await self.agent_views.overview(
            agent_id=agent_id,
            target=target,
            show_details=show_details,
            show_intents=show_intents,
            show_commands=show_commands,
            command_hash=command_hash,
            since_minutes=since_minutes,
            history=history,
            target_session_ref=target_session_ref,
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
            show_details=show_details,
            limit=limit,
            offset=offset,
        )
        if validation_error:
            return {"ok": False, "error": validation_error}
        try:
            if action == "list":
                entries = await self.context_store.list(
                    limit=limit,
                    offset=offset,
                    primary_first=limit is not None,
                )

                def compact(entry):
                    item = {"id": entry["id"], "summary": entry["summary"]}
                    if show_details:
                        item["content"] = entry["content"]
                    return item

                return {
                    "ok": True,
                    "primary": [compact(item) for item in entries if item["primary"]],
                    "additional": [compact(item) for item in entries if not item["primary"]],
                }
            if action == "create":
                entry = await self.context_store.create(summary, content, primary)
                return {"ok": True, "entry": entry}
            if action == "update":
                fields = {}
                if summary is not None:
                    fields["summary"] = summary
                if content is not None:
                    fields["content"] = content
                if primary is not None:
                    fields["primary"] = primary
                entry = await self.context_store.update(context_id, **fields)
                if entry is None:
                    return {"ok": False, "error": f"context id {context_id} not found"}
                return {"ok": True, "entry": entry}
            deleted = await self.context_store.delete(context_id)
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
        include_all_events=False,
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
            **({"include_all_events": True} if include_all_events and task_id else {}),
        )
