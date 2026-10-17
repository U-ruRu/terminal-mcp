"""Durable membership intent; each node changes one registration scope at a time."""

from __future__ import annotations

import json

from terminal_mcp.core.orchestration import utc_text


class TopologyTransitionStore:
    async def primary_handoff(self, operation_id: str) -> dict | None:
        async with self._connect("mesh_primary_read") as db:
            row = await (
                await db.execute(
                    "SELECT * FROM mesh_primary_handoffs WHERE operation_id=?", (operation_id,)
                )
            ).fetchone()
        if row is None:
            return None
        columns = (
            "operation_id",
            "mesh_id",
            "from_node_id",
            "to_node_id",
            "phase",
            "certificate_json",
            "payload_json",
            "updated_at",
        )
        result = dict(zip(columns, row, strict=True))
        result["certificate"] = json.loads(result.pop("certificate_json"))
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    async def primary_handoffs(self, *, pending_only: bool = False) -> list[dict]:
        where = " WHERE phase IN ('prepared','imported','committed')" if pending_only else ""
        async with self._connect("mesh_primary_pending") as db:
            rows = await (
                await db.execute(
                    "SELECT operation_id FROM mesh_primary_handoffs"
                    + where
                    + " ORDER BY updated_at"
                )
            ).fetchall()
        return [await self.primary_handoff(row[0]) for row in rows]

    async def prepare_primary_handoff(self, *, certificate: dict, payload: dict) -> dict:
        from terminal_mcp.fleet.control_storage import FleetControlError

        signed = certificate["payload"]
        operation_id = signed["operation_id"]
        encoded = json.dumps(certificate, sort_keys=True, separators=(",", ":"))
        async with self._connect("mesh_primary_prepare") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await db.execute(
                        "SELECT certificate_json FROM mesh_primary_handoffs WHERE operation_id=?",
                        (operation_id,),
                    )
                ).fetchone()
                if row:
                    if row[0] != encoded:
                        raise FleetControlError("mesh_operation_id_conflict")
                else:
                    pending = await (
                        await db.execute(
                            "SELECT 1 FROM mesh_primary_handoffs WHERE "
                            "phase IN ('prepared','imported','committed')"
                        )
                    ).fetchone()
                    if pending:
                        raise FleetControlError("mesh_primary_handoff_pending")
                    await db.execute(
                        "INSERT INTO mesh_primary_handoffs VALUES(?,?,?,?,'prepared',?,?,?)",
                        (
                            operation_id,
                            signed["mesh_id"],
                            signed["from_node_id"],
                            signed["to_node_id"],
                            encoded,
                            json.dumps(payload, sort_keys=True, separators=(",", ":")),
                            utc_text(),
                        ),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self.primary_handoff(operation_id)

    async def record_primary_certificates(self, certificates: list[dict]) -> None:
        """Remember a caller-verified chain without replacing any pending import."""
        from terminal_mcp.fleet.control_storage import FleetControlError

        async with self._connect("mesh_primary_certificate") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                for certificate in certificates:
                    signed = certificate["payload"]
                    encoded = json.dumps(certificate, sort_keys=True, separators=(",", ":"))
                    row = await (
                        await db.execute(
                            "SELECT certificate_json FROM mesh_primary_handoffs "
                            "WHERE operation_id=?",
                            (signed["operation_id"],),
                        )
                    ).fetchone()
                    if row:
                        if row[0] != encoded:
                            raise FleetControlError("mesh_operation_id_conflict")
                        continue
                    await db.execute(
                        "INSERT INTO mesh_primary_handoffs VALUES(?,?,?,?, 'completed',?,'{}',?)",
                        (
                            signed["operation_id"],
                            signed["mesh_id"],
                            signed["from_node_id"],
                            signed["to_node_id"],
                            encoded,
                            utc_text(),
                        ),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def mark_primary_handoff(self, operation_id: str, phase: str) -> None:
        if phase not in {"imported", "committed", "completed", "aborted"}:
            raise ValueError("invalid primary handoff phase")
        async with self._connect("mesh_primary_phase") as db:
            await db.execute(
                "UPDATE mesh_primary_handoffs SET phase=?,updated_at=?,"
                "payload_json=CASE WHEN ? IN ('completed','aborted') THEN '{}' "
                "ELSE payload_json END WHERE operation_id=?",
                (phase, utc_text(), phase, operation_id),
            )
            await db.commit()

    async def commit_primary_handoff(self, operation_id: str) -> dict:
        from terminal_mcp.fleet.control_storage import FleetControlError

        handoff = await self.primary_handoff(operation_id)
        if handoff is None:
            raise FleetControlError("mesh_primary_handoff_not_found")
        signed = handoff["certificate"]["payload"]
        stamp = utc_text()
        async with self._connect("mesh_primary_commit") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await db.execute(
                        "SELECT control_node_id,topology_revision,trust_revision,"
                        "access_policy_revision,routing_revision "
                        "FROM control_meta WHERE singleton=1"
                    )
                ).fetchone()
                if row[0] == signed["from_node_id"]:
                    if tuple(row[1:]) != tuple(signed["previous_revisions"]):
                        raise FleetControlError("topology_revision_conflict")
                    await db.execute(
                        "UPDATE control_meta SET control_node_id=?,topology_revision=?,"
                        "trust_revision=?,routing_revision=?,updated_at=? WHERE singleton=1",
                        (signed["to_node_id"], row[1] + 1, row[2] + 1, row[4] + 1, stamp),
                    )
                    await db.execute(
                        "UPDATE managed_nodes SET desired_topology_revision=?,"
                        "desired_trust_revision=?,updated_at=? WHERE state!='detached'",
                        (row[1] + 1, row[2] + 1, stamp),
                    )
                elif row[0] != signed["to_node_id"]:
                    raise FleetControlError("mesh_primary_handoff_authority_mismatch")
                await db.execute(
                    "UPDATE mesh_primary_handoffs SET phase='committed',updated_at=? "
                    "WHERE operation_id=?",
                    (stamp, operation_id),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        self.control_node_id = signed["to_node_id"]
        return await self.control_state()

    async def topology_transition(self, node_id: str) -> dict | None:
        async with self._connect("mesh_transition_read") as db:
            row = await (
                await db.execute(
                    "SELECT * FROM mesh_topology_transitions WHERE node_id=?", (node_id,)
                )
            ).fetchone()
        if row is None:
            return None
        columns = (
            "node_id",
            "operation_id",
            "target_mesh_id",
            "target_authority_id",
            "coordinator_id",
            "phase",
            "payload_json",
            "updated_at",
        )
        result = dict(zip(columns, row, strict=True))
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    async def prepare_topology_transition(
        self,
        *,
        node_id: str,
        operation_id: str,
        target_mesh_id: str | None,
        target_authority_id: str,
        coordinator_id: str,
        payload: dict,
    ) -> dict:
        from terminal_mcp.fleet.control_storage import FleetControlError

        stamp = utc_text()
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        async with self._connect("mesh_transition_prepare") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await db.execute(
                        "SELECT operation_id,target_mesh_id,target_authority_id,coordinator_id,"
                        "phase,payload_json FROM mesh_topology_transitions WHERE node_id=?",
                        (node_id,),
                    )
                ).fetchone()
                if row and row[0] == operation_id:
                    if tuple(row[1:4]) != (target_mesh_id, target_authority_id, coordinator_id):
                        raise FleetControlError("mesh_operation_id_conflict")
                    if row[4] in {"prepared", "committed"} and row[5] != encoded:
                        raise FleetControlError("mesh_operation_id_conflict")
                    if row[4] == "aborted":
                        await db.execute(
                            "UPDATE mesh_topology_transitions SET phase='prepared',"
                            "payload_json=?,updated_at=? WHERE node_id=?",
                            (encoded, stamp, node_id),
                        )
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
                        (
                            node_id,
                            operation_id,
                            target_mesh_id,
                            target_authority_id,
                            coordinator_id,
                            encoded,
                            stamp,
                        ),
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
                "UPDATE mesh_topology_transitions SET phase=?,updated_at=?,"
                "payload_json=CASE WHEN ? IN ('completed','aborted') THEN "
                "json_remove(payload_json,'$.peer','$.source_peer','$.reentry_token') "
                "ELSE payload_json END WHERE node_id=? AND operation_id=?",
                (phase, utc_text(), phase, node_id, operation_id),
            )
            await db.commit()

    async def update_transition_peer(self, node_id: str, operation_id: str, peer) -> None:
        material = json.dumps(
            {
                "instance_id": peer.instance_id,
                "origin": peer.origin,
                "public_key": peer.public_key,
                "auth_token": peer.auth_token,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        async with self._connect("mesh_transition_peer") as db:
            await db.execute(
                "UPDATE mesh_topology_transitions SET payload_json="
                "json_set(payload_json,'$.peer',json(?)),updated_at=? "
                "WHERE node_id=? AND operation_id=? AND phase='prepared'",
                (material, utc_text(), node_id, operation_id),
            )
            await db.commit()

    async def reconciliation_required(self) -> bool:
        async with self._connect("mesh_reconcile_required") as db:
            row = await (
                await db.execute(
                    "SELECT 1 FROM mesh_topology_transitions "
                    "WHERE phase IN ('prepared','committed') UNION ALL "
                    "SELECT 1 FROM mesh_primary_handoffs "
                    "WHERE phase IN ('prepared','imported','committed') UNION ALL "
                    "SELECT 1 FROM managed_nodes WHERE node_id!=? AND state!='detached' "
                    "AND (state='draining' OR (mesh_id IS NOT NULL AND mesh_id=("
                    "SELECT mesh_id FROM managed_nodes WHERE node_id=?))) LIMIT 1",
                    (self.node_id, self.node_id),
                )
            ).fetchone()
        return row is not None

    async def pending_topology_transitions(self) -> list[dict]:
        async with self._connect("mesh_transition_pending") as db:
            rows = await (
                await db.execute(
                    "SELECT node_id FROM mesh_topology_transitions "
                    "WHERE phase IN ('prepared','committed') ORDER BY node_id"
                )
            ).fetchall()
        return [await self.topology_transition(row[0]) for row in rows]
