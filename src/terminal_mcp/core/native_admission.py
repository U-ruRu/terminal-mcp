"""Recheck admitted native work inside its SQLite write transaction."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime

from terminal_mcp.core.access_mesh_grants import AccessMeshError, LocalAccessMesh
from terminal_mcp.core.access_policy_windows import merged_slot_cycle
from terminal_mcp.core.managed_sessions import FINALIZATION_OPERATIONS, ManagedOperation
from terminal_mcp.core.orchestration import parse_utc, utc_now


class NativeWriteAdmissionError(AccessMeshError):
    """The original authorization no longer permits this uncommitted write."""


@dataclass(frozen=True, slots=True)
class NativeWriteAdmission:
    namespace_id: str
    logical_agent_id: str
    work_session_id: str
    session_epoch: int
    operation: ManagedOperation
    clock: Callable[[], datetime] = utc_now


_ADMISSION: ContextVar[NativeWriteAdmission | None] = ContextVar(
    "native_write_admission", default=None
)


def current_native_write_admission() -> NativeWriteAdmission | None:
    return _ADMISSION.get()


@contextmanager
def native_write_admission(admission: NativeWriteAdmission):
    token = _ADMISSION.set(admission)
    try:
        yield
    finally:
        _ADMISSION.reset(token)


_SCOPE = (
    "SELECT namespace_id,transition_id,upgrade_pending "
    "FROM access_mesh_registry_scope WHERE singleton=1"
)
_SESSION = """
    SELECT r.*,l.work_session_id AS current_work_session_id,
           l.session_epoch AS current_session_epoch,l.cycle_started_at,
           s.logical_agent_id AS session_agent_id,s.session_epoch AS work_session_epoch,
           s.state AS session_state,s.hard_expires_at,a.state AS agent_state,
           s.authority_epoch AS session_authority_epoch,a.authority_epoch AS agent_authority_epoch,
           EXISTS(SELECT 1 FROM access_mesh_cleanup_jobs j
                  WHERE j.logical_agent_id=n.logical_agent_id
                  AND j.completed_at IS NULL) AS cleanup_pending,
           EXISTS(SELECT 1 FROM work_claim_session_fences f
                  WHERE f.logical_agent_id=n.logical_agent_id
                  AND f.work_session_id=s.work_session_id
                  AND f.session_epoch=s.session_epoch) AS session_fenced
    FROM access_mesh_native_owners n
    JOIN access_mesh_slot_replicas r USING(issuer_id,slot_id)
    JOIN access_mesh_local_sessions l USING(issuer_id,slot_id)
    JOIN logical_agent_work_sessions s ON s.work_session_id=l.work_session_id
    JOIN logical_agents a ON a.logical_agent_id=n.logical_agent_id
    WHERE n.logical_agent_id=?
"""


def _check_scope(row, admission: NativeWriteAdmission) -> None:
    if row is None:
        raise NativeWriteAdmissionError("mesh_registry_unavailable")
    if row[0] != admission.namespace_id:
        raise NativeWriteAdmissionError("mesh_namespace_mismatch")
    if row[1]:
        raise NativeWriteAdmissionError("mesh_transition_pending")
    if row[2]:
        raise NativeWriteAdmissionError("mesh_registry_migrating")


def _check_session(row, description, admission: NativeWriteAdmission) -> dict:
    if row is None:
        raise NativeWriteAdmissionError("session_not_active")
    data = dict(zip((column[0] for column in description), row, strict=True))
    if (
        data["logical_agent_id"] != admission.logical_agent_id
        or data["session_agent_id"] != admission.logical_agent_id
        or data["current_work_session_id"] != admission.work_session_id
        or int(data["current_session_epoch"]) != admission.session_epoch
        or int(data["work_session_epoch"]) != admission.session_epoch
        or data["session_authority_epoch"] != data["agent_authority_epoch"]
    ):
        raise NativeWriteAdmissionError("stale_session_epoch")
    if (
        data["session_state"] != "active"
        or data["agent_state"] not in {"active", "armed"}
        or data["session_fenced"]
    ):
        raise NativeWriteAdmissionError("session_not_active")
    if data["cleanup_pending"]:
        raise NativeWriteAdmissionError("access_mesh_cleanup_pending")
    return data


def _check_cycle(data, cycle, admission: NativeWriteAdmission, now: datetime) -> None:
    if (
        cycle["state"] not in {"active", "warning", "draining"}
        or now >= parse_utc(data["hard_expires_at"])
    ):
        raise NativeWriteAdmissionError("session_expired")
    if parse_utc(cycle["started_at"]) != parse_utc(data["cycle_started_at"]):
        raise NativeWriteAdmissionError("stale_session_epoch")
    if cycle["state"] == "draining" and admission.operation not in FINALIZATION_OPERATIONS:
        raise NativeWriteAdmissionError("session_draining")



_NUMBER_CLAIMS = (
    "SELECT * FROM access_mesh_number_claims WHERE number=("
    "SELECT number FROM access_mesh_number_claims WHERE issuer_id=? AND slot_id=?)"
)


def _rows(values, description):
    return [dict(zip((column[0] for column in description), row, strict=True)) for row in values]


def _number_group(data, claims):
    from terminal_mcp.storage.access_mesh_numbers import MeshSessionNumbers

    for group in MeshSessionNumbers._groups(claims):
        keys = {(row["issuer_id"], row["slot_id"]) for row in group}
        if (data["issuer_id"], data["slot_id"]) in keys:
            return MeshSessionNumbers._group_winner(group), keys
    return None


def _cycle(slot, now, winner, keys, starts, ends):
    starts = [row for row in starts if (row["issuer_id"], row["slot_id"]) in keys]
    ends = [row for row in ends if (row["issuer_id"], row["slot_id"]) in keys]
    latest_start = max(
        starts,
        key=lambda row: (row["active_from"], row["issuer_id"], row["slot_id"], row["event_id"]),
        default=None,
    )
    latest_end = max(ends, key=lambda row: (row["ended_at"], row["event_id"]), default=None)
    return merged_slot_cycle(
        LocalAccessMesh._snapshot(slot),
        now,
        winner=winner,
        latest_start=latest_start,
        latest_end=latest_end,
    )


def assert_native_write_admission(db, *, admission: NativeWriteAdmission | None = None) -> None:
    admission = admission if admission is not None else current_native_write_admission()
    if admission is None:
        return
    if not db.in_transaction:
        raise RuntimeError("native admission requires a write transaction")
    _check_scope(db.execute(_SCOPE).fetchone(), admission)
    cursor = db.execute(_SESSION, (admission.logical_agent_id,))
    data = _check_session(cursor.fetchone(), cursor.description, admission)
    now = admission.clock()

    def read(sql, parameters):
        cursor = db.execute(sql, parameters)
        return _rows(cursor.fetchall(), cursor.description)

    group = _number_group(data, read(_NUMBER_CLAIMS, (data["issuer_id"], data["slot_id"])))
    if group is None:
        cycle = merged_slot_cycle(LocalAccessMesh._snapshot(data), now)
    else:
        winner, keys = group
        slots = read(
            "SELECT * FROM access_mesh_slot_replicas WHERE issuer_id=? AND slot_id=?",
            (winner["issuer_id"], winner["slot_id"]),
        )
        if not slots:
            raise NativeWriteAdmissionError("session_not_active")
        starts = read("SELECT * FROM access_mesh_number_starts WHERE number=?", (winner["number"],))
        ends = read("SELECT * FROM access_mesh_number_ends WHERE number=?", (winner["number"],))
        cycle = _cycle(slots[0], now, winner, keys, starts, ends)
    _check_cycle(data, cycle, admission, now)


async def assert_native_write_admission_async(
    db, *, admission: NativeWriteAdmission | None = None
) -> None:
    admission = admission if admission is not None else current_native_write_admission()
    if admission is None:
        return
    if not db.in_transaction:
        raise RuntimeError("native admission requires a write transaction")
    cursor = await db.execute(_SCOPE)
    _check_scope(await cursor.fetchone(), admission)
    cursor = await db.execute(_SESSION, (admission.logical_agent_id,))
    data = _check_session(await cursor.fetchone(), cursor.description, admission)
    now = admission.clock()

    async def read(sql, parameters):
        cursor = await db.execute(sql, parameters)
        return _rows(await cursor.fetchall(), cursor.description)

    group = _number_group(data, await read(_NUMBER_CLAIMS, (data["issuer_id"], data["slot_id"])))
    if group is None:
        cycle = merged_slot_cycle(LocalAccessMesh._snapshot(data), now)
    else:
        winner, keys = group
        slots = await read(
            "SELECT * FROM access_mesh_slot_replicas WHERE issuer_id=? AND slot_id=?",
            (winner["issuer_id"], winner["slot_id"]),
        )
        if not slots:
            raise NativeWriteAdmissionError("session_not_active")
        starts = await read(
            "SELECT * FROM access_mesh_number_starts WHERE number=?", (winner["number"],)
        )
        ends = await read(
            "SELECT * FROM access_mesh_number_ends WHERE number=?", (winner["number"],)
        )
        cycle = _cycle(slots[0], now, winner, keys, starts, ends)
    _check_cycle(data, cycle, admission, now)
