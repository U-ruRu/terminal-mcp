from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ClaimOwnerKind = Literal["logical_agent"]
PersistentSlotState = Literal["suspended", "armed", "active", "stopping", "deleting", "deleted"]
WorkSessionState = Literal["active", "stopping", "ended", "expired", "suspended", "failed"]


@dataclass(frozen=True, slots=True)
class ClaimOwner:
    kind: ClaimOwnerKind
    owner_id: str

    def __post_init__(self) -> None:
        if self.kind != "logical_agent":
            raise ValueError(f"unsupported claim owner kind: {self.kind}")
        if not self.owner_id or not self.owner_id.strip():
            raise ValueError("claim owner id is required")

    @classmethod
    def logical_agent(cls, logical_agent_id: str) -> ClaimOwner:
        return cls("logical_agent", logical_agent_id)


@dataclass(frozen=True, slots=True)
class PersistentSlot:
    logical_agent_id: str
    display_name: str
    state: PersistentSlotState
    authority_node_id: str
    authority_epoch: int
    slot_revision: int
    selector_generation: int
    auth_generation: int
    created_at: str
    updated_at: str
    deleted_at: str | None = None
    tombstone_reason: str | None = None


@dataclass(frozen=True, slots=True)
class WorkSessionRecord:
    work_session_id: str
    logical_agent_id: str
    session_epoch: int
    authority_node_id: str
    authority_epoch: int
    started_at: str
    hard_expires_at: str
    auth_principal_id: str | None
    auth_generation: int
    state: WorkSessionState
    origin_instance_id: str | None = None
    ended_at: str | None = None
    end_reason: str | None = None
