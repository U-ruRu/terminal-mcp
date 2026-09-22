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
    started_at: str | None = None
    finished_at: str | None = None
    queue_id: int | None = None
    queue_sequence: int | None = None
    enqueued_at: str | None = None
    claimed_at: str | None = None


@dataclass(slots=True)
class Line:
    seq: int
    cmd_hash: str
    appeared_at: str
    text: str
