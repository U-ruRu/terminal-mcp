from dataclasses import dataclass
from typing import Literal

Status = Literal["queued", "running", "completed", "failed", "cancelled"]


@dataclass(slots=True)
class Command:
    cmd_hash: str
    cmd: str
    status: Status
    pid: int | None = None
    exit_code: int | None = None
    error: str | None = None


@dataclass(slots=True)
class Line:
    seq: int
    cmd_hash: str
    appeared_at: str
    text: str
