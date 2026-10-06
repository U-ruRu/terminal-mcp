"""Transport-independent ports for atomic, single-database application mutations.

Only the repositories yielded by ``transaction()`` participate in that transaction.
Legacy per-call stores, remote authority requests and output-cache writes do not.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol, TypeAlias, TypedDict, Unpack, runtime_checkable

from terminal_mcp.core.persistent_agents import WorkSessionRecord

JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]


class ContextEntry(TypedDict):
    id: int
    summary: str
    content: str
    primary: bool


class ContextPatch(TypedDict, total=False):
    summary: str
    content: str
    primary: bool


@dataclass(frozen=True)
class TaskSnapshot:
    namespace: str
    task_id: str
    title: str
    state: str
    revision: int


@dataclass(frozen=True)
class CommandSnapshot:
    command_hash: str
    status: str
    queue_id: int | None
    queue_sequence: int | None


@runtime_checkable
class ContextRepositoryPort(Protocol):
    async def get(self, context_id: int) -> ContextEntry | None: ...

    async def create(self, summary: str, content: str, primary: bool) -> ContextEntry: ...

    async def update(
        self, context_id: int, **patch: Unpack[ContextPatch]
    ) -> ContextEntry | None: ...

    async def delete(self, context_id: int) -> bool: ...


@runtime_checkable
class SessionRepositoryPort(Protocol):
    async def get_work_session(self, work_session_id: str) -> WorkSessionRecord | None: ...

    async def activity(
        self, agent_id: str, tool: str, now: str, command_hash: str | None = None
    ) -> None: ...

    async def add_audit_event(
        self,
        logical_agent_id: str,
        event_type: str,
        *,
        principal_id: str,
        now: str,
        work_session_id: str | None = None,
        session_epoch: int | None = None,
        payload: Mapping[str, JsonValue] | None = None,
    ) -> int: ...


@runtime_checkable
class TaskRepositoryPort(Protocol):
    async def get_snapshot(self, namespace: str, task_id: str) -> TaskSnapshot | None: ...

    async def add_event(
        self,
        namespace: str,
        task_id: str,
        event_type: str,
        *,
        now: str,
        agent_id: str | None = None,
        payload: Mapping[str, JsonValue] | None = None,
        logical_agent_id: str | None = None,
        work_session_id: str | None = None,
        session_epoch: int | None = None,
    ) -> int: ...


@runtime_checkable
class CommandRepositoryPort(Protocol):
    async def get_snapshot(self, command_hash: str) -> CommandSnapshot | None: ...

    async def record_attribution(
        self,
        command_hash: str,
        agent_id: str,
        *,
        now: str,
        command_type: str,
        command_preview: str,
        logical_agent_id: str | None = None,
        work_session_id: str | None = None,
        session_epoch: int | None = None,
    ) -> None: ...


@dataclass(frozen=True)
class ApplicationRepositories:
    """Transaction-scoped repositories; no connection or per-repository commit is exposed."""

    context: ContextRepositoryPort
    sessions: SessionRepositoryPort
    tasks: TaskRepositoryPort
    commands: CommandRepositoryPort


@runtime_checkable
class ApplicationUnitOfWorkPort(Protocol):
    def transaction(self) -> AbstractAsyncContextManager[ApplicationRepositories]:
        """Commit a successful scope; roll back an exception before commit.

        The scope belongs to one asyncio task and one authoritative database.
        Nested transactions and use after scope exit must fail immediately.
        External effects and legacy store calls must stay outside this scope.
        """
        ...
