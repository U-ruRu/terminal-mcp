from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Literal

from terminal_mcp.core.orchestration import CROCKFORD

ClaimOwnerKind = Literal["legacy_session", "logical_agent"]
PersistentSlotState = Literal["suspended", "armed", "active", "stopping", "deleting", "deleted"]
WorkSessionState = Literal["active", "stopping", "ended", "expired", "suspended", "failed"]


@dataclass(frozen=True, slots=True)
class ClaimOwner:
    kind: ClaimOwnerKind
    owner_id: str

    def __post_init__(self) -> None:
        if self.kind not in {"legacy_session", "logical_agent"}:
            raise ValueError(f"unsupported claim owner kind: {self.kind}")
        if not self.owner_id or not self.owner_id.strip():
            raise ValueError("claim owner id is required")

    @classmethod
    def legacy_session(cls, agent_id: str) -> ClaimOwner:
        return cls("legacy_session", agent_id)

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
class ArmGeneration:
    logical_agent_id: str
    generation: int
    armed_at: str
    armed_until: str
    captured_duration_seconds: int
    selector_generation: int
    auth_generation: int
    slot_revision: int
    consumed_at: str | None = None
    revoked_at: str | None = None


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


def normalize_slot_selector(value: str) -> str:
    selector = (value or "").strip().upper()
    if len(selector) != 4 or any(char not in CROCKFORD for char in selector):
        raise ValueError("slot selector must be exactly 4 Crockford Base32 characters")
    return selector


def generate_slot_selector() -> str:
    return "".join(secrets.choice(CROCKFORD) for _ in range(4))
