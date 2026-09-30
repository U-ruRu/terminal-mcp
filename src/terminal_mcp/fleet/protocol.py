from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

FLEET_PROTOCOL_MAJOR = 1
MAX_SOURCE_REPLAY_LIMIT = 1000
SOURCE_CAPABILITIES = frozenset(
    {
        "fleet.source.v1",
        "fleet.source.snapshot-barrier.v1",
        "fleet.source.generation-reset.v1",
        "fleet.source.current-recovery.v2",
        "fleet.source.current-entity.v2",
        "fleet.source.query.v2",
        "fleet.source.sampled-resources.v2",
    }
)

AUTHORITY_CAPABILITIES = frozenset(
    {
        "fleet.authority.v1",
        "fleet.authority.monotonic-permit.v1",
        "fleet.authority.message-gate.v1",
        "fleet.authority.transfer.v1",
    }
)

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class FleetProtocolError(ValueError):
    pass


def validate_protocol_id(value: str, label: str) -> str:
    normalized = str(value).strip()
    if not _ID_RE.fullmatch(normalized):
        raise FleetProtocolError(
            f"{label} must be 1-128 ASCII letters, digits, dot, underscore, colon or dash"
        )
    return normalized


def validate_protocol_major(value: int) -> int:
    major = int(value)
    if major != FLEET_PROTOCOL_MAJOR:
        raise FleetProtocolError(
            f"unsupported fleet protocol major {major}; expected {FLEET_PROTOCOL_MAJOR}"
        )
    return major


def validate_capabilities(values: list[str] | tuple[str, ...] | set[str]) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple, set)):
        raise FleetProtocolError("capabilities must be a collection")
    normalized: list[str] = []
    for item in values:
        capability = str(item).strip()
        if not capability or len(capability) > 128 or not _ID_RE.fullmatch(capability):
            raise FleetProtocolError(f"invalid fleet capability: {item!r}")
        normalized.append(capability)
    return tuple(sorted(set(normalized)))


def canonical_event_id(
    fleet_id: str,
    node_id: str,
    source_stream_generation: str,
    source_seq: int,
) -> str:
    fleet_id = validate_protocol_id(fleet_id, "fleet_id")
    node_id = validate_protocol_id(node_id, "node_id")
    generation = validate_protocol_id(source_stream_generation, "source_stream_generation")
    seq = int(source_seq)
    if seq < 1:
        raise FleetProtocolError("source_seq must be positive")
    raw = json.dumps(
        [fleet_id, node_id, generation, seq],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class SourceEvent:
    event_id: str
    fleet_id: str
    node_id: str
    source_stream_generation: str
    source_seq: int
    event_type: str
    entity_type: str
    entity_id: str
    entity_revision: int
    payload_version: int
    payload: dict[str, Any]
    created_at: str
    authority_node_id: str | None = None
    authority_epoch: int | None = None

    def __post_init__(self) -> None:
        validate_protocol_id(self.fleet_id, "fleet_id")
        validate_protocol_id(self.node_id, "node_id")
        validate_protocol_id(self.source_stream_generation, "source_stream_generation")
        if self.source_seq < 1 or self.entity_revision < 1 or self.payload_version < 1:
            raise FleetProtocolError(
                "source_seq, entity_revision and payload_version must be positive"
            )
        if (
            not self.event_type.strip()
            or not self.entity_type.strip()
            or not self.entity_id.strip()
        ):
            raise FleetProtocolError("event_type, entity_type and entity_id are required")
        expected = canonical_event_id(
            self.fleet_id,
            self.node_id,
            self.source_stream_generation,
            self.source_seq,
        )
        if self.event_id != expected:
            raise FleetProtocolError("event_id does not match canonical source identity")
        if self.authority_epoch is not None and self.authority_epoch < 1:
            raise FleetProtocolError("authority_epoch must be positive")

    def to_dict(self) -> dict[str, Any]:
        result = {
            "event_id": self.event_id,
            "fleet_id": self.fleet_id,
            "node_id": self.node_id,
            "source_stream_generation": self.source_stream_generation,
            "source_seq": self.source_seq,
            "event_type": self.event_type,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "entity_revision": self.entity_revision,
            "payload_version": self.payload_version,
            "payload": self.payload,
            "created_at": self.created_at,
        }
        if self.authority_node_id is not None:
            result["authority_node_id"] = self.authority_node_id
        if self.authority_epoch is not None:
            result["authority_epoch"] = self.authority_epoch
        return result
