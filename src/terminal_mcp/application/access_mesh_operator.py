"""Operator-only provisioning, per-slot controls and durable default policy."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict
from datetime import datetime

from terminal_mcp.core.access_mesh_grants import AccessMeshError, SlotPolicy
from terminal_mcp.core.read_contract import bounded_page, decode_cursor, encode_cursor
from terminal_mcp.storage.access_mesh import local_cycle, public_name


class AccessMeshOperator:
    def __init__(self, mesh):
        self.mesh = mesh
        self.store = mesh.store

    @staticmethod
    def require_operator(actor, *, write=False):
        if actor.endpoint_role != "operator":
            raise AccessMeshError("access_denied")
        admission = actor.admission()
        if admission is None:
            raise AccessMeshError("persistent_auth_required")
        admission.require("terminal:execute" if write else "terminal:read")

    def _view(self, slot):
        return {
            "issuer_node_id": slot.issuer_id,
            "slot_id": slot.slot_id,
            "logical_agent_id": slot.logical_agent_id,
            "public_name": public_name(slot.issuer_id, slot.logical_agent_id),
            "mode": slot.kind,
            "state": slot.state,
            "revision": slot.revision,
            "policy": asdict(slot.policy),
            "session_lifecycle": local_cycle(slot, self.mesh.clock()),
        }

    def _spec(self, actor, key, payload):
        request_key = hashlib.sha256(
            json.dumps(
                ["operator-mesh-v2", self.store.local_node_id, actor.principal_id, key],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return {"request_key": request_key, "fingerprint": fingerprint, "connection_key": None}

    def _policy(self, values, base):
        try:
            return SlotPolicy(**{**asdict(base), **(values or {})})
        except (TypeError, ValueError) as exc:
            raise AccessMeshError("access_mesh_invalid_policy") from exc

    async def slots(self, actor, *, limit=20, cursor=None):
        self.require_operator(actor)
        scope = {"kind": "access.operator.slots", "node": self.store.local_node_id}
        after = decode_cursor(cursor, scope)
        rows = await asyncio.to_thread(self.store.issuer_slots, after=after, limit=limit + 1)
        page, _ = bounded_page(
            [self._view(slot) for _, slot in rows], limit=limit, cursor=None, scope=scope
        )
        next_cursor = (
            encode_cursor(rows[len(page) - 1][0], scope) if page and len(rows) > len(page) else None
        )
        return {"ok": True, "slots": page, "next_cursor": next_cursor}

    async def get(self, actor, *, slot_id):
        self.require_operator(actor)
        slot = await asyncio.to_thread(self.store.slot, self.store.local_node_id, slot_id)
        if slot is None:
            raise AccessMeshError("access_mesh_slot_not_found")
        return {"ok": True, "slot": self._view(slot)}

    async def defaults(self, actor):
        self.require_operator(actor)
        current = await asyncio.to_thread(self.store.defaults)
        return {
            "ok": True,
            **(
                current
                or {
                    "revision": 1,
                    "policy": asdict(self.mesh.defaults),
                    "legacy_enabled": self.mesh.legacy_enabled,
                }
            ),
        }

    async def mutate(
        self,
        actor,
        *,
        action,
        idempotency_key,
        slot_id=None,
        expected_revision=None,
        mode=None,
        policy=None,
        deadline_at=None,
        legacy_enabled=None,
    ):
        self.require_operator(actor, write=True)
        if action not in {
            "create",
            "defaults",
            "policy",
            "suspend",
            "resume",
            "delete",
            "rotate",
            "deadline",
            "end",
        }:
            raise AccessMeshError("invalid_request")
        payload = {
            "action": action,
            "slot_id": slot_id,
            "expected_revision": expected_revision,
            "mode": mode,
            "policy": policy,
            "deadline_at": deadline_at,
            "legacy_enabled": legacy_enabled,
        }
        spec = self._spec(actor, idempotency_key, payload)
        async with self.mesh._issuer_session_lock:
            previous = await asyncio.to_thread(
                self.store.receipt, spec["request_key"], spec["fingerprint"]
            )
            if previous is not None:
                return previous
            if action == "create":
                resolved = self._policy(policy, self.mesh.defaults)
                return await self.mesh.issue(
                    actor, kind=mode, policy=resolved, start=mode == "legacy", receipt_spec=spec
                )
            if action == "defaults":
                current = await self.defaults(actor)
                resolved = self._policy(policy, SlotPolicy(**current["policy"]))
                enabled = current["legacy_enabled"] if legacy_enabled is None else legacy_enabled
                result = {
                    "ok": True,
                    "issuer_node_id": self.store.local_node_id,
                    "slot_id": "defaults",
                    "revision": expected_revision + 1,
                    "policy": asdict(resolved),
                    "legacy_enabled": enabled,
                }
                receipt = self.store.prepare_receipt(event_id="defaults", result=result, **spec)
                await asyncio.to_thread(
                    self.store.change_defaults,
                    expected_revision=expected_revision,
                    policy=asdict(resolved),
                    legacy_enabled=enabled,
                    receipt=receipt,
                )
                self.mesh.defaults, self.mesh.legacy_enabled = resolved, enabled
                return result
            slot = await asyncio.to_thread(self.store.slot, self.store.local_node_id, slot_id)
            if slot is None:
                raise AccessMeshError("access_mesh_slot_not_found")
            if slot.revision != expected_revision:
                raise AccessMeshError("revision_conflict")
            changes = {
                "policy": "SlotPolicyChanged",
                "suspend": "SlotSuspended",
                "resume": "SlotResumed",
                "delete": "SlotDeleted",
                "rotate": "AccessCodeRotated",
                "deadline": "SessionUpdated",
                "end": "SessionEnded",
            }
            resolved, start, deadline = None, None, None
            if action == "policy":
                resolved = self._policy(policy, slot.policy)
            if action == "deadline":
                cycle = local_cycle(slot, self.mesh.clock())
                if cycle["state"] not in {"active", "warning", "draining"}:
                    raise AccessMeshError("session_expired")
                try:
                    start = datetime.fromisoformat(cycle["started_at"])
                    deadline = datetime.fromisoformat(deadline_at)
                    if deadline.tzinfo is None or deadline <= start:
                        raise ValueError("invalid deadline")
                except (TypeError, ValueError) as exc:
                    raise AccessMeshError("access_mesh_invalid_time") from exc
            return await self.mesh.change(
                actor,
                slot_id=slot.slot_id,
                kind=changes[action],
                expected_revision=expected_revision,
                policy=resolved,
                effective_at=start,
                deadline_at=deadline,
                receipt_spec=spec,
            )
