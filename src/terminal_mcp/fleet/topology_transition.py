"""Durable membership intent; each node changes one registration scope at a time."""

from __future__ import annotations

import json

from terminal_mcp.core.orchestration import utc_text


class TopologyTransitionStore:
    async def topology_transition(self, node_id: str) -> dict | None:
        async with self._connect("mesh_transition_read") as db:
            row = await (await db.execute(
                "SELECT * FROM mesh_topology_transitions WHERE node_id=?", (node_id,)
            )).fetchone()
        if row is None:
            return None
        columns = ("node_id", "operation_id", "target_mesh_id", "target_authority_id",
                   "coordinator_id", "phase", "payload_json", "updated_at")
        result = dict(zip(columns, row, strict=True))
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    async def prepare_topology_transition(
        self, *, node_id: str, operation_id: str, target_mesh_id: str | None,
        target_authority_id: str, coordinator_id: str, payload: dict,
    ) -> dict:
        from terminal_mcp.fleet.control_storage import FleetControlError

        stamp = utc_text()
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        async with self._connect("mesh_transition_prepare") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await (await db.execute(
                    "SELECT operation_id,target_mesh_id,target_authority_id,coordinator_id,"
                    "phase,payload_json FROM mesh_topology_transitions WHERE node_id=?",
                    (node_id,),
                )).fetchone()
                if row and row[0] == operation_id:
                    if tuple(row[1:4]) != (target_mesh_id, target_authority_id, coordinator_id) \
                            or row[5] != encoded:
                        raise FleetControlError("mesh_operation_id_conflict")
                elif row and row[4] in {"prepared", "committed"}:
                    raise FleetControlError("mesh_transition_pending")
                else:
                    await db.execute(
                        "INSERT INTO mesh_topology_transitions(node_id,operation_id,"
                        "target_mesh_id,target_authority_id,coordinator_id,phase,payload_json,"
                        "updated_at) VALUES(?,?,?,?,?,'prepared',?,?) "
                        "ON CONFLICT(node_id) DO UPDATE SET operation_id=excluded.operation_id,"
                        "target_mesh_id=excluded.target_mesh_id,"
                        "target_authority_id=excluded.target_authority_id,"
                        "coordinator_id=excluded.coordinator_id,phase='prepared',"
                        "payload_json=excluded.payload_json,updated_at=excluded.updated_at",
                        (node_id, operation_id, target_mesh_id, target_authority_id,
                         coordinator_id, encoded, stamp),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self.topology_transition(node_id)

    async def complete_topology_transition(
        self, node_id: str, operation_id: str, phase: str = "completed"
    ) -> None:
        if phase not in {"committed", "completed", "aborted"}:
            raise ValueError("invalid topology transition phase")
        async with self._connect("mesh_transition_complete") as db:
            await db.execute(
                "UPDATE mesh_topology_transitions SET phase=?,updated_at=? "
                "WHERE node_id=? AND operation_id=?",
                (phase, utc_text(), node_id, operation_id),
            )
            await db.commit()

    async def pending_topology_transitions(self) -> list[dict]:
        async with self._connect("mesh_transition_pending") as db:
            rows = await (await db.execute(
                "SELECT node_id FROM mesh_topology_transitions "
                "WHERE phase IN ('prepared','committed') ORDER BY node_id"
            )).fetchall()
        return [await self.topology_transition(row[0]) for row in rows]
