"""Transport-independent command and context request contracts.

The Access-code variants are compatibility inputs retained until the managed
identity migration. Authenticated principal/provider/endpoint metadata never
belongs in these models.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from terminal_mcp.application.input_limits import (
    MAX_COMMAND_CHARS,
    MAX_CONTEXT_CONTENT_CHARS,
    MAX_CONTEXT_SUMMARY_CHARS,
    MAX_HASH_CHARS,
    MAX_OPAQUE_CURSOR_CHARS,
    MAX_QUEUE_ID,
    MAX_SQLITE_INTEGER,
    MAX_TASK_SCOPE_CHARS,
)
from terminal_mcp.core.read_contract import (
    DEFAULT_CMD_READ_LINES,
    DEFAULT_PAGE_LIMIT,
    MAX_CMD_READ_LINES,
    MAX_PAGE_LIMIT,
)


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CmdReadRequest(_StrictRequest):
    action: Literal["read"]
    code: Annotated[str | None, Field(min_length=4, max_length=4, pattern=r"^[0-9]{4}$")] = None
    cmd_hash: Annotated[str, Field(min_length=1, max_length=MAX_HASH_CHARS)]
    limit: Annotated[int, Field(ge=1, le=MAX_CMD_READ_LINES)] = DEFAULT_CMD_READ_LINES
    cursor: Annotated[str | None, Field(max_length=MAX_OPAQUE_CURSOR_CHARS)] = None


class CmdRunRequest(_StrictRequest):
    action: Literal["run"]
    code: Annotated[str | None, Field(min_length=4, max_length=4, pattern=r"^[0-9]{4}$")] = None
    command: Annotated[str, Field(min_length=1, max_length=MAX_COMMAND_CHARS)]
    queue_id: Annotated[int | None, Field(ge=1, le=MAX_QUEUE_ID)] = None
    task_scope: Annotated[str, Field(min_length=1, max_length=MAX_TASK_SCOPE_CHARS)] = "none"


class CmdCancelRequest(_StrictRequest):
    action: Literal["cancel"]
    code: Annotated[str | None, Field(min_length=4, max_length=4, pattern=r"^[0-9]{4}$")] = None
    cmd_hash: Annotated[str, Field(min_length=1, max_length=MAX_HASH_CHARS)]


class CmdRecoveryRequest(_StrictRequest):
    action: Literal["recovery"]
    code: Annotated[str | None, Field(min_length=4, max_length=4, pattern=r"^[0-9]{4}$")] = None
    command: Annotated[str, Field(min_length=1, max_length=MAX_COMMAND_CHARS)]


CmdRequest = Annotated[
    CmdReadRequest | CmdRunRequest | CmdCancelRequest | CmdRecoveryRequest,
    Field(discriminator="action"),
]


class ContextListRequest(_StrictRequest):
    action: Literal["list"]
    detail: Literal["summary", "full"] = "summary"
    limit: Annotated[int, Field(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_PAGE_LIMIT
    cursor: Annotated[str | None, Field(max_length=MAX_OPAQUE_CURSOR_CHARS)] = None


class ContextCreateRequest(_StrictRequest):
    action: Literal["create"]
    code: Annotated[str | None, Field(min_length=4, max_length=4, pattern=r"^[0-9]{4}$")] = None
    summary: Annotated[str, Field(min_length=1, max_length=MAX_CONTEXT_SUMMARY_CHARS)]
    content: Annotated[str, Field(min_length=1, max_length=MAX_CONTEXT_CONTENT_CHARS)]
    primary: bool = False


class ContextUpdateRequest(_StrictRequest):
    action: Literal["update"]
    code: Annotated[str | None, Field(min_length=4, max_length=4, pattern=r"^[0-9]{4}$")] = None
    context_id: Annotated[int, Field(ge=1, le=MAX_SQLITE_INTEGER)]
    summary: Annotated[str | None, Field(min_length=1, max_length=MAX_CONTEXT_SUMMARY_CHARS)] = None
    content: Annotated[str | None, Field(min_length=1, max_length=MAX_CONTEXT_CONTENT_CHARS)] = None
    primary: bool | None = None


class ContextDeleteRequest(_StrictRequest):
    action: Literal["delete"]
    code: Annotated[str | None, Field(min_length=4, max_length=4, pattern=r"^[0-9]{4}$")] = None
    context_id: Annotated[int, Field(ge=1, le=MAX_SQLITE_INTEGER)]


ContextRequest = Annotated[
    ContextListRequest | ContextCreateRequest | ContextUpdateRequest | ContextDeleteRequest,
    Field(discriminator="action"),
]
