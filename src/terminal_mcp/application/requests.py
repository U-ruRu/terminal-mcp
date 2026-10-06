"""Transport-independent command and context request contracts.

The Access-code variants are compatibility inputs retained until the managed
identity migration. Authenticated principal/provider/endpoint metadata never
belongs in these models.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

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
    cmd_hash: str
    limit: Annotated[int, Field(ge=1, le=MAX_CMD_READ_LINES)] = DEFAULT_CMD_READ_LINES
    cursor: str | None = None


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
    detail: Literal["summary", "full"] = "summary"
    limit: Annotated[int, Field(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_PAGE_LIMIT
    cursor: str | None = None


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
