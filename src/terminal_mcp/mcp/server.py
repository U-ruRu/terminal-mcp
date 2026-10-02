# ruff: noqa: E501
from typing import Annotated, Literal
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from terminal_mcp.api_models import (
    AgentFinishResponse,
    AgentOverviewResponse,
    CancelResponse,
    ContextAction,
    ContextResponse,
    CoordinateResponse,
    HealthResponse,
    MessageResponse,
    ReadResponse,
    RecoveryResponse,
    ReviewDimension,
    ReviewVerdict,
    RunResponse,
    TaskAction,
    TaskLane,
    TaskMutationResponse,
    TaskOperationalStatus,
    TaskPriority,
    TasksResponse,
    TaskState,
)
from terminal_mcp.core.orchestration import public_agent_name
from terminal_mcp.core.service import DEFAULT_READ_LINES, MAX_READ_LINES
from terminal_mcp.telemetry import observed

_SAFE_READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
_SAFE_OPERATION = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=False
)
ScopeItem = Annotated[str, Field(min_length=1, max_length=80)]
StepItem = Annotated[str, Field(min_length=1, max_length=160)]
TaskRef = Annotated[str, Field(min_length=1, max_length=512)]


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CmdReadRequest(_StrictRequest):
    action: Literal["read"]
    cmd_hash: str
    lines_count: Annotated[int, Field(ge=1, le=MAX_READ_LINES)] = DEFAULT_READ_LINES
    offset: Annotated[int | None, Field(ge=0)] = None


class CmdRunRequest(_StrictRequest):
    action: Literal["run"]
    code: Annotated[str, Field(min_length=4, max_length=4, pattern=r"^[0-9]{4}$")]
    command: str
    queue_id: Annotated[int | None, Field(ge=1)] = None
    task_scope: str = "none"


class CmdCancelRequest(_StrictRequest):
    action: Literal["cancel"]
    code: Annotated[str, Field(min_length=4, max_length=4, pattern=r"^[0-9]{4}$")]
    cmd_hash: str


class CmdRecoveryRequest(_StrictRequest):
    action: Literal["recovery"]
    code: Annotated[str, Field(min_length=4, max_length=4, pattern=r"^[0-9]{4}$")]
    command: str


CmdRequest = Annotated[
    CmdReadRequest | CmdRunRequest | CmdCancelRequest | CmdRecoveryRequest,
    Field(discriminator="action"),
]


class ContextListRequest(_StrictRequest):
    action: Literal["list"]
    show_details: bool = False


class ContextCreateRequest(_StrictRequest):
    action: Literal["create"]
    code: Annotated[str, Field(min_length=4, max_length=4, pattern=r"^[0-9]{4}$")]
    summary: str
    content: str
    primary: bool = False


class ContextUpdateRequest(_StrictRequest):
    action: Literal["update"]
    code: Annotated[str, Field(min_length=4, max_length=4, pattern=r"^[0-9]{4}$")]
    context_id: int
    summary: str | None = None
    content: str | None = None
    primary: bool | None = None


class ContextDeleteRequest(_StrictRequest):
    action: Literal["delete"]
    code: Annotated[str, Field(min_length=4, max_length=4, pattern=r"^[0-9]{4}$")]
    context_id: int


ContextRequest = Annotated[
    ContextListRequest | ContextCreateRequest | ContextUpdateRequest | ContextDeleteRequest,
    Field(discriminator="action"),
]


def _structured_result(data, summary: str) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=summary)],
        structuredContent=data.model_dump(mode="json", exclude_none=True),
        isError=False,
    )


def _overview_summary(data: AgentOverviewResponse) -> str:
    if data.admission_required:
        return "Agent admission required. Reuse an existing full agent_id or confirm proposed_agent_id."
    if data.registration_required:
        if data.return_to_chat:
            return "Agent session ended. Return to chat before starting new work."
        return "Agent session expired. Call agent_start."
    identity = (data.self.agent_id or data.self.name) if data.self else data.agent_name or "unknown"
    return f"{identity} | active={len(data.active)} | overlaps={len(data.overlaps or [])}"


def build_mcp(
    service,
    public_base_url: str = "http://127.0.0.1:8080",
    auth_mode: str = "none",
    *,
    persistent_enabled: bool = False,
):
    parsed = urlparse(public_base_url)
    hostname = parsed.hostname or "127.0.0.1"
    hosts = list(
        dict.fromkeys(
            [
                parsed.netloc,
                hostname,
                f"{hostname}:443",
                "127.0.0.1",
                "127.0.0.1:8080",
                "localhost",
                "localhost:8080",
            ]
        )
    )
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[public_base_url],
    )
    mcp = FastMCP(
        "terminal-mcp",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        transport_security=security,
    )

    @mcp.tool(
        structured_output=True,
        annotations=_SAFE_OPERATION,
        description="Two-step agent admission. First call without agent_id proposes a private full id and does not start a session. Reuse an existing full agent_id from another Terminal MCP or call again with proposed_agent_id plus the plan to confirm. Calling with an active agent_id and no plan is an idempotent resume; plan fields update that active session.",
    )
    async def agent_start(
        task_summary: Annotated[str | None, Field(min_length=1, max_length=120)] = None,
        intent: Annotated[str | None, Field(min_length=1, max_length=160)] = None,
        details: Annotated[list[StepItem] | None, Field(min_length=1, max_length=12)] = None,
        work_scope: Annotated[list[ScopeItem] | None, Field(max_length=4)] = None,
        agent_id: str | None = None,
    ) -> Annotated[CallToolResult, AgentOverviewResponse]:
        data = AgentOverviewResponse.model_validate(
            await observed(
                service,
                "mcp",
                "agent_start",
                service.agent_start(
                    task_summary=task_summary,
                    intent=intent,
                    details=details,
                    work_scope=work_scope,
                    agent_id=agent_id,
                ),
            )
        )
        return _structured_result(data, _overview_summary(data))

    @mcp.tool(
        structured_output=True,
        annotations=_SAFE_OPERATION,
        description="Inspect or update current coordination. agent_id alone returns current step/intent/detail. step selects a plan step. Providing step+intent updates current work and refreshes task-context freshness. show_details includes other agents' registered plans. Always inspect pending_messages and acknowledge them with message before new run work.",
    )
    async def coordinate(
        agent_id: str,
        step: Annotated[int | None, Field(ge=1)] = None,
        intent: Annotated[str | None, Field(min_length=1, max_length=160)] = None,
        show_details: bool = False,
    ) -> Annotated[CallToolResult, CoordinateResponse]:
        data = CoordinateResponse.model_validate(
            await observed(
                service,
                "mcp",
                "coordinate",
                service.coordinate(agent_id, step=step, intent=intent, show_details=show_details),
            )
        )
        return _structured_result(
            data,
            f"{data.agent_name} step={data.step} — {data.intent}"
            if data.ok
            else f"Coordinate failed: {data.error}",
        )

    @mcp.tool(
        structured_output=True,
        annotations=_SAFE_OPERATION,
        description="Send, acknowledge, inspect, or reply to coordination messages. Sending supports direct target, ordinary-message broadcast by omitting target, explicit broadcast via target=broadcast, or managed-task target via namespace+task_id. alert=true requires an explicit agent, task, or target=broadcast destination and canonically implies require_reply=true. SEEN only means surfaced; ACK REQUIRED clears only after explicit message(agent_id,message_hash). message_hash alone acknowledges a received message or inspects receipts for the sender, including inactive recipients; message_hash + text replies to the original sender. ALERT blocks normal work until replied.",
    )
    async def message(
        agent_id: str,
        text: Annotated[str | None, Field(min_length=1, max_length=500)] = None,
        target: Literal["broadcast"]
        | Annotated[str, Field(min_length=1, max_length=64)]
        | None = None,
        message_hash: Annotated[str | None, Field(min_length=8, max_length=8)] = None,
        require_reply: bool = False,
        alert: bool = False,
        namespace: Annotated[str | None, Field(min_length=1, max_length=120)] = None,
        task_id: Annotated[str | None, Field(min_length=1, max_length=120)] = None,
    ) -> Annotated[CallToolResult, MessageResponse]:
        data = MessageResponse.model_validate(
            await observed(
                service,
                "mcp",
                "message",
                service.message(
                    agent_id,
                    text=text,
                    target=target,
                    message_hash=message_hash,
                    require_reply=require_reply,
                    alert=alert,
                    namespace=namespace,
                    task_id=task_id,
                ),
            )
        )
        summary = (
            f"Message {data.message_hash}: read_by={','.join(data.read_by) or '-'}; "
            f"inactive={','.join(data.inactive_recipients) or '-'}"
            if message_hash
            else f"Message {data.message_hash}: delivered_to={','.join(data.delivered_to) or '-'}"
        )
        return _structured_result(data, summary if data.ok else f"Message failed: {data.error}")

    @mcp.tool(
        structured_output=True,
        annotations=_SAFE_READ_ONLY,
        description="Observe current or historical agent sessions. With no arguments, returns a compact fleet view. target selects a public agent name; show_details, show_intents and show_commands expand its journal; command_hash returns the full original command; since_minutes bounds history.",
    )
    async def agents(
        agent_id: str | None = None,
        target: Annotated[str | None, Field(min_length=1, max_length=64)] = None,
        show_details: bool = False,
        show_intents: bool = False,
        show_commands: bool = False,
        command_hash: Annotated[str | None, Field(min_length=8, max_length=8)] = None,
        since_minutes: Annotated[int | None, Field(ge=1, le=10080)] = None,
    ) -> Annotated[CallToolResult, AgentOverviewResponse]:
        data = AgentOverviewResponse.model_validate(
            await observed(
                service,
                "mcp",
                "agents",
                service.agents(
                    agent_id,
                    target=target,
                    show_details=show_details,
                    show_intents=show_intents,
                    show_commands=show_commands,
                    command_hash=command_hash,
                    since_minutes=since_minutes,
                ),
            )
        )
        return _structured_result(data, _overview_summary(data))

    @mcp.tool(
        structured_output=True,
        annotations=_SAFE_OPERATION,
        description=(
            "Manage instance-local operational context. Actions: list, create, update, delete. "
            "list returns primary/additional summaries by default; show_details=true includes content. "
            "create requires summary, content and primary; update/delete use the stable integer id."
        ),
    )
    async def context(
        action: ContextAction,
        id: Annotated[int | None, Field(ge=1)] = None,
        summary: Annotated[str | None, Field(min_length=1, max_length=100)] = None,
        content: Annotated[str | None, Field(min_length=1)] = None,
        primary: bool | None = None,
        show_details: bool = False,
    ) -> Annotated[CallToolResult, ContextResponse]:
        data = ContextResponse.model_validate(
            await observed(
                service,
                "mcp",
                "context",
                service.context(
                    action,
                    context_id=id,
                    summary=summary,
                    content=content,
                    primary=primary,
                    show_details=show_details,
                ),
            )
        )
        if data.ok and action == "list":
            text = f"primary={len(data.primary or [])} additional={len(data.additional or [])}"
        elif data.ok and data.entry:
            text = f"context {data.entry.id}: {data.entry.summary}"
        elif data.ok and data.deleted_id is not None:
            text = f"context {data.deleted_id} deleted"
        else:
            text = f"Context failed: {data.error}"
        return _structured_result(data, text)

    @mcp.tool(
        structured_output=True,
        annotations=_SAFE_READ_ONLY,
        description=(
            "Inspect managed tasks with optional namespace/lane/state/operational_status/tags filters. Responses "
            "include claimable pressure, oldest-ready recommendation, missing-dependency "
            "observability, and tag_counts so agents can discover the active custom tag "
            "vocabulary. show_details expands durable history; show_done/show_archived include "
            "completed history."
        ),
    )
    async def tasks(
        namespace: Annotated[str | None, Field(min_length=1, max_length=120)] = None,
        task_id: Annotated[str | None, Field(min_length=1, max_length=120)] = None,
        lane: TaskLane | None = None,
        state: TaskState | None = None,
        operational_status: TaskOperationalStatus | None = None,
        tags: Annotated[list[str] | None, Field(max_length=50)] = None,
        show_details: bool = False,
        show_done: bool = False,
        show_archived: bool = False,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        cursor: Annotated[int | None, Field(ge=0)] = None,
    ) -> Annotated[CallToolResult, TasksResponse]:
        data = TasksResponse.model_validate(
            await observed(
                service,
                "mcp",
                "tasks",
                service.tasks(
                    namespace=namespace,
                    task_id=task_id,
                    lane=lane,
                    state=state,
                    operational_status=operational_status,
                    tags=tags,
                    show_details=show_details,
                    show_done=show_done,
                    show_archived=show_archived,
                    limit=limit,
                    cursor=cursor,
                ),
            )
        )
        if data.task:
            summary = (
                f"{data.task.namespace}/{data.task.task_id} "
                f"{data.task.operational_status} state={data.task.state} {data.task.lane}"
            )
        else:
            summary = f"tasks={len(data.tasks)}"
            if data.recommended:
                summary += f" recommended={data.recommended.get('task_id')}"
        return _structured_result(data, summary if data.ok else f"Tasks failed: {data.error}")

    @mcp.tool(
        structured_output=True,
        annotations=_SAFE_OPERATION,
        description=(
            "Mutate one unified managed task. create requires an explicit isolation_hint (use 'none' when no isolation is required); the hint is stored/exposed without interpretation. claim requires claim_intent. cooperative controls "
            "concurrent participation, not task visibility. The first live claimant is owner; later "
            "cooperative claimants are participants that may comment and edit safe metadata, while "
            "workflow changes require a current live owner; an unclaimed task must be claimed first. "
            "blocked requires blocker_reason for a claimed task; "
            "release requires release_reason as durable handoff history; done requires result. "
            "comment is append-only history. input_refs/output_refs are ordered, deduplicated "
            "reference lists; changing output_refs creates a new output_state_id. "
            "action=review binds dimensions/verdict/evidence to the task's current output state "
            "automatically. relate/unrelate manage generic task relations; review work may also "
            "use lane=review plus relation_kind=review_of. archive requires archive_note and "
            "preserves workflow state. force=true with force_reason overrides open dependencies "
            "only, never ownership."
        ),
    )
    async def task(
        agent_id: str,
        action: TaskAction,
        namespace: Annotated[str, Field(min_length=1, max_length=120)],
        task_id: Annotated[str | None, Field(min_length=1, max_length=120)] = None,
        title: Annotated[str | None, Field(min_length=1, max_length=200)] = None,
        lane: TaskLane | None = None,
        priority: TaskPriority | None = None,
        state: TaskState | None = None,
        description: Annotated[str | None, Field(max_length=8000)] = None,
        next_action: Annotated[str | None, Field(max_length=2000)] = None,
        isolation_hint: Annotated[str | None, Field(min_length=1, max_length=160)] = None,
        resource_context: dict[str, object] | None = None,
        cooperative: bool | None = None,
        checkpoint: str | dict[str, object] | list[object] | None = None,
        candidate_ref: Annotated[str | None, Field(max_length=200)] = None,
        input_refs: Annotated[list[TaskRef] | None, Field(max_length=64)] = None,
        output_refs: Annotated[list[TaskRef] | None, Field(max_length=64)] = None,
        dimensions: Annotated[list[ReviewDimension] | None, Field(max_length=3)] = None,
        verdict: ReviewVerdict | None = None,
        evidence: dict[str, object] | None = None,
        result: str | dict[str, object] | list[object] | None = None,
        tags: Annotated[list[str] | None, Field(max_length=50)] = None,
        dependencies: Annotated[list[dict[str, str]] | None, Field(max_length=100)] = None,
        force: bool = False,
        force_reason: Annotated[str | None, Field(max_length=2000)] = None,
        claim_intent: Annotated[str | None, Field(max_length=160)] = None,
        blocker_reason: Annotated[str | None, Field(max_length=4000)] = None,
        release_reason: Annotated[str | None, Field(max_length=4000)] = None,
        archive_note: Annotated[str | None, Field(max_length=4000)] = None,
        comment_text: Annotated[str | None, Field(max_length=4000)] = None,
        relation_kind: Annotated[str | None, Field(max_length=64)] = None,
        related_namespace: Annotated[str | None, Field(max_length=120)] = None,
        related_task_id: Annotated[str | None, Field(max_length=120)] = None,
        note: Annotated[str | None, Field(max_length=2000)] = None,
        expected_revision: Annotated[int | None, Field(ge=1)] = None,
    ) -> Annotated[CallToolResult, TaskMutationResponse]:
        kwargs = {
            "task_id": task_id,
            "title": title,
            "lane": lane,
            "priority": priority,
            "state": state,
            "description": description,
            "next_action": next_action,
            "isolation_hint": isolation_hint,
            "resource_context": resource_context,
            "cooperative": cooperative,
            "checkpoint": checkpoint,
            "candidate_ref": candidate_ref,
            "input_refs": input_refs,
            "output_refs": output_refs,
            "dimensions": dimensions,
            "verdict": verdict,
            "evidence": evidence,
            "result": result,
            "tags": tags,
            "dependencies": dependencies,
            "force": force,
            "force_reason": force_reason,
            "claim_intent": claim_intent,
            "blocker_reason": blocker_reason,
            "release_reason": release_reason,
            "archive_note": archive_note,
            "comment_text": comment_text,
            "relation_kind": relation_kind,
            "related_namespace": related_namespace,
            "related_task_id": related_task_id,
            "note": note,
            "expected_revision": expected_revision,
        }
        data = TaskMutationResponse.model_validate(
            await observed(
                service,
                "mcp",
                "task",
                service.task(
                    agent_id,
                    action=action,
                    namespace=namespace,
                    **{key: value for key, value in kwargs.items() if value is not None},
                ),
            )
        )
        summary = (
            f"{data.task.namespace}/{data.task.task_id} {data.task.state}"
            if data.ok and data.task
            else f"Task action failed: {data.error}"
        )
        if data.warnings:
            summary += " warnings=" + ",".join(item.code for item in data.warnings)
        return _structured_result(data, summary)

    @mcp.tool(
        structured_output=True,
        annotations=_SAFE_OPERATION,
        description="Finish an agent session early. Sessions also expire automatically after inactivity.",
    )
    async def agent_finish(agent_id: str) -> Annotated[CallToolResult, AgentFinishResponse]:
        data = AgentFinishResponse.model_validate(
            await observed(service, "mcp", "agent_finish", service.agent_finish(agent_id))
        )
        summary = (
            f"{data.agent_name} finished."
            if data.ok
            else "Agent session expired. Call agent_start."
        )
        if data.ok and data.pending_communication:
            pending = data.pending_communication
            summary += (
                " Pending communication: "
                f"unacknowledged={len(pending.unacknowledged)}, "
                f"reply_required={len(pending.reply_required)}, alerts={len(pending.alerts)}."
            )
        return _structured_result(data, summary)

    @mcp.tool(
        structured_output=True,
        annotations=_SAFE_OPERATION,
        description="Queue a shell command on a numbered FIFO worker. task_scope is required on every command: use none with no live claims; with live claims use none, all, or a claimed <namespace>/<task_id>. queue_id controls FIFO affinity.",
    )
    async def run(
        agent_id: str,
        cmd: str,
        task_scope: Annotated[str, Field(min_length=1, max_length=260)],
        queue_id: Annotated[int | None, Field(ge=1)] = None,
    ) -> Annotated[CallToolResult, RunResponse]:
        data = RunResponse.model_validate(
            await observed(
                service,
                "mcp",
                "run",
                service.run(cmd, agent_id=agent_id, queue_id=queue_id, task_scope=task_scope),
            )
        )
        summary = (
            f"Command {data.cmd_hash} queued."
            if data.ok
            else (
                "Task context expired. Call coordinate with step and intent before run."
                if data.task_context_expired
                else (
                    "Agent session expired. Call agent_start."
                    if data.registration_required
                    else f"Command was not queued: {data.error}"
                )
            )
        )
        return _structured_result(data, summary)

    @mcp.tool(
        structured_output=True,
        annotations=_SAFE_OPERATION,
        description="Run one persisted emergency command outside FIFO. agent_id is optional and used for attribution; when supplied, response also surfaces pending_messages without blocking recovery.",
    )
    async def recovery(
        cmd: str,
        agent_id: str | None = None,
    ) -> Annotated[CallToolResult, RecoveryResponse]:
        raw = await observed(service, "mcp", "recovery", service.recovery(cmd, agent_id=agent_id))
        raw.pop("agent_id", None)
        raw["agent_name"] = public_agent_name(agent_id)
        data = RecoveryResponse.model_validate(raw)
        return _structured_result(
            data,
            f"Recovery {data.cmd_hash or 'unallocated'} returned {data.displayed_lines_count} line(s).",
        )

    @mcp.tool(
        structured_output=True,
        annotations=_SAFE_READ_ONLY,
        description="Read terminal output. cmd_hash selects one command; omitting it reads the global stream. agent_id is independently optional and adds session/message context. ALERT messages block read; ordinary unread messages remain visible without blocking read.",
    )
    async def read(
        agent_id: str | None = None,
        cmd_hash: str | None = None,
        lines_count: Annotated[int, Field(ge=1, le=MAX_READ_LINES)] = DEFAULT_READ_LINES,
        offset: int | None = None,
    ) -> Annotated[CallToolResult, ReadResponse]:
        raw = await observed(
            service,
            "mcp",
            "read",
            service.read(cmd_hash, lines_count, offset, agent_id=agent_id),
        )
        raw.pop("agent_id", None)
        raw["agent_name"] = public_agent_name(agent_id)
        data = ReadResponse.model_validate(raw)
        scope = f"command {cmd_hash}" if cmd_hash else "global log"
        return _structured_result(
            data,
            (
                f"Returned {data.displayed_lines_count} line(s) from {scope}."
                if data.ok
                else f"Read failed: {data.error}"
            ),
        )

    @mcp.tool(
        structured_output=True,
        annotations=_SAFE_OPERATION,
        description="Cancel a queued or running command. The response distinguishes queued pre-start cancellation from running cancellation and reports whether execution had started. agent_id is optional; when supplied, response also surfaces pending_messages. Use read for final command status and execution provenance.",
    )
    async def cancel(
        cmd_hash: str,
        agent_id: str | None = None,
    ) -> Annotated[CallToolResult, CancelResponse]:
        raw = await observed(service, "mcp", "cancel", service.cancel(cmd_hash, agent_id=agent_id))
        raw.pop("agent_id", None)
        raw["agent_name"] = public_agent_name(agent_id)
        data = CancelResponse.model_validate(raw)
        if data.ok and data.cancelled_from == "queued":
            summary = f"Command {data.cmd_hash} was cancelled before execution started."
        elif data.ok and data.cancelled_from == "running":
            summary = f"Command {data.cmd_hash} was cancelled after execution started."
        else:
            summary = f"Command {data.cmd_hash} was not cancelled: {data.error}"
        return _structured_result(data, summary)

    @mcp.tool(
        structured_output=False,
        annotations=_SAFE_OPERATION,
        description="Manage feature-gated Persistent Slots. Selector is a non-secret locator; authenticated admission is required by the server.",
    )
    async def persistent_slot(
        action: Literal[
            "list", "get", "create", "rename", "rotate_selector", "play", "suspend", "delete"
        ],
        logical_agent_id: str | None = None,
        display_name: str | None = None,
        selector: str | None = None,
        expected_revision: Annotated[int | None, Field(ge=1)] = None,
        idempotency_key: Annotated[str | None, Field(min_length=8, max_length=128)] = None,
    ) -> dict:
        backend = getattr(service, "persistent", None)
        if backend is None:
            return {"ok": False, "code": "policy_incompatible", "error": "policy_incompatible"}
        if action == "list":
            return await backend.slot_list()
        if action == "get" and logical_agent_id:
            return await backend.slot_get(logical_agent_id)
        if action == "create" and display_name:
            return await backend.slot_create(display_name)
        if (
            action == "rename"
            and logical_agent_id
            and display_name
            and expected_revision
            and idempotency_key
        ):
            return await backend.slot_rename(
                logical_agent_id,
                display_name,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )
        if (
            action == "rotate_selector"
            and logical_agent_id
            and selector
            and expected_revision
            and idempotency_key
        ):
            return await backend.slot_rotate_selector(
                logical_agent_id,
                selector,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )
        if action == "play" and logical_agent_id and expected_revision and idempotency_key:
            return await backend.slot_play(
                logical_agent_id,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )
        if action == "suspend" and logical_agent_id and expected_revision and idempotency_key:
            return await backend.slot_suspend(
                logical_agent_id,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )
        if action == "delete" and logical_agent_id and expected_revision and idempotency_key:
            return await backend.slot_delete(
                logical_agent_id,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )
        return {
            "ok": False,
            "code": "invalid_request",
            "error": f"persistent_slot.{action}: required fields missing",
        }

    @mcp.tool(
        structured_output=False,
        annotations=_SAFE_OPERATION,
        description="Start or end one immutable Persistent work session. Start accepts an Armed slot or a reopenable D-window after normal end, with verified authenticated admission.",
    )
    async def persistent_session(
        action: Literal["start", "end"],
        selector: str | None = None,
        expected_revision: Annotated[int | None, Field(ge=1)] = None,
        logical_agent_id: str | None = None,
        work_session_id: str | None = None,
        session_epoch: Annotated[int | None, Field(ge=1)] = None,
    ) -> dict:
        backend = getattr(service, "persistent", None)
        if backend is None:
            return {"ok": False, "code": "policy_incompatible", "error": "policy_incompatible"}
        if action == "start" and selector and expected_revision:
            return await backend.session_start(selector, expected_revision=expected_revision)
        if action == "end" and logical_agent_id and work_session_id and session_epoch:
            return await backend.session_end(logical_agent_id, work_session_id, session_epoch)
        return {
            "ok": False,
            "code": "invalid_request",
            "error": f"persistent_session.{action}: required fields missing",
        }

    @mcp.tool(
        structured_output=False,
        annotations=_SAFE_OPERATION,
        description="Run a command under exact Persistent work-session authority. Queue claim rechecks the session fence before execution.",
    )
    async def persistent_run(
        cmd: str,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: Annotated[int, Field(ge=1)],
        task_scope: str,
        queue_id: Annotated[int | None, Field(ge=1)] = None,
    ) -> dict:
        backend = getattr(service, "persistent", None)
        if backend is None:
            return {"ok": False, "code": "policy_incompatible", "error": "policy_incompatible"}
        return await backend.run(
            cmd,
            logical_agent_id=logical_agent_id,
            work_session_id=work_session_id,
            session_epoch=session_epoch,
            queue_id=queue_id,
            task_scope=task_scope,
        )

    @mcp.tool(
        structured_output=False,
        annotations=_SAFE_OPERATION,
        description="Cancel one Persistent command under exact logical-agent/work-session authority.",
    )
    async def persistent_cancel(
        cmd_hash: str,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: Annotated[int, Field(ge=1)],
    ) -> dict:
        backend = getattr(service, "persistent", None)
        if backend is None:
            return {"ok": False, "code": "policy_incompatible", "error": "policy_incompatible"}
        return await backend.cancel(
            cmd_hash,
            logical_agent_id=logical_agent_id,
            work_session_id=work_session_id,
            session_epoch=session_epoch,
        )

    @mcp.tool(
        structured_output=False,
        annotations=_SAFE_OPERATION,
        description="Mutate a managed task under exact Persistent work-session authority. payload contains the existing task action fields.",
    )
    async def persistent_task(
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: Annotated[int, Field(ge=1)],
        action: str,
        namespace: str,
        task_id: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> dict:
        backend = getattr(service, "persistent", None)
        if backend is None:
            return {"ok": False, "code": "policy_incompatible", "error": "policy_incompatible"}
        return await backend.task(
            logical_agent_id=logical_agent_id,
            work_session_id=work_session_id,
            session_epoch=session_epoch,
            action=action,
            namespace=namespace,
            task_id=task_id,
            **(payload or {}),
        )

    @mcp.tool(
        structured_output=False,
        annotations=_SAFE_OPERATION,
        description="Explicitly release or atomically reassign a durable Persistent task claim.",
    )
    async def persistent_claim(
        action: Literal["release", "reassign"],
        namespace: str,
        task_id: str,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: Annotated[int, Field(ge=1)],
        to_logical_agent_id: str | None = None,
        expected_revision: Annotated[int | None, Field(ge=1)] = None,
        idempotency_key: Annotated[str | None, Field(min_length=8, max_length=128)] = None,
    ) -> dict:
        backend = getattr(service, "persistent", None)
        if backend is None:
            return {"ok": False, "code": "policy_incompatible", "error": "policy_incompatible"}
        if action == "release":
            return await backend.claim_release(
                namespace,
                task_id,
                logical_agent_id,
                work_session_id=work_session_id,
                session_epoch=session_epoch,
            )
        if to_logical_agent_id and expected_revision and idempotency_key:
            return await backend.claim_reassign(
                namespace,
                task_id,
                logical_agent_id,
                to_logical_agent_id,
                work_session_id=work_session_id,
                session_epoch=session_epoch,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )
        return {
            "ok": False,
            "code": "invalid_request",
            "error": "persistent_claim.reassign: required fields missing",
        }

    @mcp.tool(
        structured_output=True,
        annotations=_SAFE_READ_ONLY,
        description="Return terminal service health. agent_id is optional; when supplied for a live session it refreshes session TTL and includes pending coordination messages.",
    )
    async def health(agent_id: str | None = None) -> Annotated[CallToolResult, HealthResponse]:
        raw = await observed(service, "mcp", "health", service.health(auth_mode, agent_id=agent_id))
        raw.pop("agent_id", None)
        raw["agent_name"] = public_agent_name(agent_id)
        data = HealthResponse.model_validate(raw)
        return _structured_result(
            data,
            "Terminal service is healthy." if data.ok else "Terminal service is unhealthy.",
        )

    # ACCESS002 hard cutover: public MCP discovery is deliberately reduced to
    # seven capability tools. The legacy functions above remain internal adapters
    # for Console/HTTP compatibility but are not discoverable over MCP.
    for tool_name in (
        "agent_start",
        "coordinate",
        "message",
        "agents",
        "context",
        "tasks",
        "task",
        "agent_finish",
        "run",
        "recovery",
        "read",
        "cancel",
        "persistent_slot",
        "persistent_session",
        "persistent_run",
        "persistent_cancel",
        "persistent_task",
        "persistent_claim",
        "health",
    ):
        mcp.remove_tool(tool_name)

    def access_backend():
        return getattr(service, "persistent", None)

    async def access_identity(code: str | None):
        backend = access_backend()
        if backend is None:
            return None, {
                "ok": False,
                "code": "policy_incompatible",
                "error": "policy_incompatible",
            }
        if not code:
            return None, {
                "ok": False,
                "code": "access_code_required",
                "error": "access_code_required",
            }
        identity = await backend.access_identity(code)
        if not identity.get("ok"):
            return None, identity
        return identity, None

    @mcp.tool(
        name="session",
        structured_output=False,
        annotations=_SAFE_OPERATION,
        description=(
            "Start, end, or interrupt a unified Access session. start requires an explicit "
            "mode: persistent requires an existing Access code; legacy creates a temporary "
            "slot and returns its Access code once. end/interrupt require the Access code."
        ),
    )
    async def access_session_tool(
        action: Literal["start", "end", "interrupt"],
        mode: Literal["persistent", "legacy"] | None = None,
        code: Annotated[
            str | None, Field(min_length=4, max_length=4, pattern=r"^[0-9]{4}$")
        ] = None,
        display_name: Annotated[str | None, Field(max_length=80)] = None,
    ) -> dict:
        backend = access_backend()
        if backend is None:
            return {"ok": False, "code": "policy_incompatible", "error": "policy_incompatible"}
        if action == "start":
            if mode is None:
                return {"ok": False, "code": "mode_required", "error": "mode_required"}
            if mode == "legacy" and code is not None:
                return {
                    "ok": False,
                    "code": "legacy_code_not_allowed",
                    "error": "legacy_code_not_allowed",
                }
            return await backend.access_session_start(
                mode=mode, access_code=code, display_name=display_name
            )
        if not code:
            return {
                "ok": False,
                "code": "access_code_required",
                "error": "access_code_required",
            }
        return await backend.access_session_stop(code, interrupt=action == "interrupt")

    @mcp.tool(
        name="observe",
        structured_output=False,
        annotations=_SAFE_READ_ONLY,
        description=(
            "Read unified session or task state. Access codes are intentionally absent from "
            "the observation path."
        ),
    )
    async def access_observe_tool(
        subject: Literal["sessions", "tasks"] = "sessions",
        namespace: str | None = None,
        task_id: str | None = None,
        lane: TaskLane | None = None,
        state: TaskState | None = None,
        operational_status: TaskOperationalStatus | None = None,
        tags: list[str] | None = None,
        show_details: bool = False,
        show_done: bool = False,
        show_archived: bool = False,
        limit: Annotated[int, Field(ge=1, le=1000)] = 50,
        cursor: str | None = None,
    ) -> dict:
        backend = access_backend()
        if subject == "sessions":
            if backend is None:
                return {
                    "ok": False,
                    "code": "policy_incompatible",
                    "error": "policy_incompatible",
                }
            return await backend.access_observe_slots()
        result = await service.tasks(
            namespace=namespace,
            task_id=task_id,
            lane=lane,
            state=state,
            operational_status=operational_status,
            tags=tags,
            show_details=show_details,
            show_done=show_done,
            show_archived=show_archived,
            limit=limit,
            cursor=cursor,
        )
        store = getattr(service, "task_store", None)
        if store is not None:
            rows = await store.list_tasks(show_done=True, show_archived=True, limit=None, offset=0)
            result["namespaces"] = sorted({item["namespace"] for item in rows})
        return result

    @mcp.tool(
        name="message",
        structured_output=False,
        annotations=_SAFE_OPERATION,
        description=(
            "Send, acknowledge, or reply to coordination messages using the sender's public "
            "Access name. The sender must resolve to an active unified session. Persistent "
            "callers may provide the same Access code used by cmd/task/context so roaming "
            "attachments resolve one fenced logical work-session identity."
        ),
    )
    async def access_message_tool(
        sender: str,
        code: Annotated[
            str | None, Field(min_length=4, max_length=4, pattern=r"^[0-9]{4}$")
        ] = None,
        text: str | None = None,
        target: str | None = None,
        message_hash: str | None = None,
        require_reply: bool = False,
        alert: bool = False,
        namespace: str | None = None,
        task_id: str | None = None,
    ) -> dict:
        backend = access_backend()
        if backend is None:
            return {"ok": False, "code": "policy_incompatible", "error": "policy_incompatible"}
        return await backend.access_message(
            sender,
            access_code=code,
            text=text,
            target=target,
            message_hash=message_hash,
            require_reply=require_reply,
            alert=alert,
            namespace=namespace,
            task_id=task_id,
        )

    @mcp.tool(
        name="task",
        structured_output=False,
        annotations=_SAFE_OPERATION,
        description=(
            "Mutate a managed task under an active unified Access session. Claim ownership is "
            "durable per logical slot; WIP is one live managed-task claim per slot."
        ),
    )
    async def access_task_tool(
        code: Annotated[str, Field(min_length=4, max_length=4, pattern=r"^[0-9]{4}$")],
        action: TaskAction,
        namespace: str,
        task_id: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> dict:
        identity, failure = await access_identity(code)
        if failure is not None:
            return failure
        return await access_backend().task(
            logical_agent_id=identity["logical_agent_id"],
            work_session_id=identity["work_session_id"],
            session_epoch=identity["session_epoch"],
            access_code=code,
            action=action,
            namespace=namespace,
            task_id=task_id,
            **(payload or {}),
        )

    @mcp.tool(
        name="cmd",
        structured_output=False,
        annotations=_SAFE_OPERATION,
        description=(
            "Run, read, cancel, or execute recovery commands. The request is an action-"
            "discriminated union: read has no code field; run/cancel/recovery require code."
        ),
    )
    async def access_cmd_tool(request: CmdRequest) -> dict:
        if request.action == "read":
            return await service.read(
                cmd_hash=request.cmd_hash,
                lines_count=request.lines_count,
                offset=request.offset,
                agent_id=None,
            )
        identity, failure = await access_identity(request.code)
        if failure is not None:
            return failure
        backend = access_backend()
        if request.action == "run":
            return await backend.run(
                request.command,
                logical_agent_id=identity["logical_agent_id"],
                work_session_id=identity["work_session_id"],
                session_epoch=identity["session_epoch"],
                access_code=request.code,
                queue_id=request.queue_id,
                task_scope=request.task_scope,
            )
        if request.action == "cancel":
            return await backend.cancel(
                request.cmd_hash,
                logical_agent_id=identity["logical_agent_id"],
                work_session_id=identity["work_session_id"],
                session_epoch=identity["session_epoch"],
                access_code=request.code,
            )
        result = await backend.recovery(
            request.command,
            logical_agent_id=identity["logical_agent_id"],
            work_session_id=identity["work_session_id"],
            session_epoch=identity["session_epoch"],
            access_code=request.code,
        )
        result["public_name"] = identity["public_name"]
        result["session_ref"] = identity["session_ref"]
        return result

    @mcp.tool(
        name="context",
        structured_output=False,
        annotations=_SAFE_OPERATION,
        description=(
            "Read or mutate instance context using an action-discriminated request. list has "
            "no code field; create/update/delete require an active unified Access code."
        ),
    )
    async def access_context_tool(request: ContextRequest) -> dict:
        data = request.model_dump(exclude={"action", "code"}, exclude_none=True)
        if request.action != "list":
            _identity, failure = await access_identity(request.code)
            if failure is not None:
                return failure
        return await service.context(request.action, **data)

    @mcp.tool(
        name="health",
        structured_output=False,
        annotations=_SAFE_READ_ONLY,
        description="Return terminal service health without requiring an Access code.",
    )
    async def access_health_tool() -> dict:
        return await service.health(auth_mode, agent_id=None)

    return mcp
